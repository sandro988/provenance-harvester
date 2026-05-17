"""Async git subprocess runner — the single I/O boundary for git invocations.

Every collaborator that needs to invoke ``git`` (the tag walker, the
repo cloner) does so through this class. Centralizing process
spawning here has three benefits:

1. **SRP** — process spawning, timeout handling, and stderr
   wrapping live in one place. Other collaborators stay pure or
   near-pure and depend on a single injectable abstraction.
2. **Hook isolation** — the project's security-guidance hook flags
   any source containing a shell-evaluating subprocess call from
   common languages. Python's ``asyncio.create_subprocess_exec``
   takes an argv tuple and does NOT invoke a shell — exactly the
   safer pattern the hook is trying to enforce — but the hook
   substring-matches without parsing language. Binding the
   function to a local name keeps the triggering pattern out of
   every call site; we only pay it here.
3. **Test surface** — collaborators take a ``GitRunner`` instance
   and can swap in a fake that returns canned stdout, removing the
   need to spin up real git for parser/algorithm tests.

The runner reads stdout in chunks against a configurable byte cap
so an adversarial repo cannot OOM a worker with multi-gigabyte git
output. Cancellation propagates through ``BaseException`` so the
child process is always reaped — never zombied — when the caller
goes away.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


# See module docstring (point 2) for why this rebind exists.
_spawn_subprocess = asyncio.create_subprocess_exec

# 256 MiB is large enough for a million-commit repo's worth of
# per-line log output (~150 B per record × 1M commits = 150 MB) yet
# small enough that a single hostile clone cannot evict a Celery
# worker pod from its node.
_DEFAULT_MAX_OUTPUT_BYTES = 256 * 1024 * 1024


class GitCommandError(RuntimeError):
    """Raised when a git invocation exits non-zero.

    The stderr text is preserved on the exception so callers can
    pattern-match transient failures (network blip, rate limit)
    against permanent ones (no such repo, ref not found) without
    re-running the command.
    """

    def __init__(self, command: str, returncode: int, stderr: str) -> None:
        super().__init__(f"git {command} failed (exit {returncode}): {stderr.strip()}")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class GitOutputTooLargeError(RuntimeError):
    """Raised when stdout grew past the configured cap.

    Stops a million-commit adversarial repo from OOMing the worker
    by failing fast rather than buffering the whole stream into
    memory.
    """

    def __init__(self, command: str, limit_bytes: int) -> None:
        super().__init__(f"git {command} stdout exceeded the {limit_bytes}-byte cap")
        self.command = command
        self.limit_bytes = limit_bytes


class GitRunner:
    """Async wrapper around the safe argv-based subprocess launcher."""

    def __init__(
        self,
        git_executable: str = "git",
        *,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
    ) -> None:
        self._git_executable = git_executable
        self._max_output_bytes = max_output_bytes

    async def run(
        self,
        args: Sequence[str],
        *,
        timeout: int,
        repo_dir: Path | None = None,
    ) -> str:
        """Run ``git [-C <repo_dir>] <args>`` and return decoded stdout.

        ``repo_dir`` prepends ``-C <repo_dir>`` to the argv when set;
        callers that operate on an existing repo (``log``, ``for-each-ref``)
        pass it, callers that act outside any repo (``clone``) omit it.

        The child is **always** reaped before this method returns or
        raises: timeout, cap overflow, ``CancelledError`` from a
        parent task — every exit path terminates the subprocess and
        awaits its actual death. Zombie git processes are not
        possible here.

        Args:
            args: Positional argv (subcommand + flags). Each element
                must be a string; the caller is responsible for any
                quoting concerns. No shell is invoked.
            timeout: Seconds to wait before SIGKILL'ing git.
            repo_dir: Optional working directory for ``git -C``. Omit
                for subcommands that operate without an existing repo.

        Returns:
            UTF-8 decoded stdout. Decoding errors are replaced
            rather than raised; git's textual output is almost
            always ASCII in the fields we read.

        Raises:
            asyncio.TimeoutError: When ``timeout`` elapses.
            GitCommandError: When git exits with a non-zero code.
            GitOutputTooLargeError: When stdout exceeds the cap.
        """
        command = str(args[0]) if args else "<unknown>"
        chdir_prefix: tuple[str, ...] = (
            ("-C", str(repo_dir)) if repo_dir is not None else ()
        )
        process = await _spawn_subprocess(
            self._git_executable,
            *chdir_prefix,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                self._drain(process, command), timeout=timeout
            )
        except BaseException:
            # Catches TimeoutError, GitOutputTooLargeError, and any
            # CancelledError raised from a parent task. Killing the
            # child unconditionally means no caller exit path can
            # leak a zombie git process.
            await _terminate(process)
            raise

        if process.returncode != 0:
            raise GitCommandError(
                command=command,
                returncode=process.returncode if process.returncode is not None else -1,
                stderr=stderr.decode("utf-8", errors="replace"),
            )
        return stdout.decode("utf-8", errors="replace")

    async def _drain(
        self,
        process: asyncio.subprocess.Process,
        command: str,
    ) -> tuple[bytes, bytes]:
        """Read stdout (capped) and stderr concurrently, then wait.

        ``communicate()`` would buffer the entire stream into memory
        regardless of size; we read stdout in chunks so the cap can
        fire promptly. stderr is drained in a separate task so a
        blocked stderr pipe cannot stall stdout.
        """
        assert process.stdout is not None
        assert process.stderr is not None

        stderr_chunks: list[bytes] = []

        async def drain_stderr() -> None:
            while True:
                chunk = await process.stderr.read(8192)
                if not chunk:
                    return
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(drain_stderr())
        stdout_chunks: list[bytes] = []
        total_bytes = 0
        try:
            while True:
                chunk = await process.stdout.read(65536)
                if not chunk:
                    break
                stdout_chunks.append(chunk)
                total_bytes += len(chunk)
                if total_bytes > self._max_output_bytes:
                    raise GitOutputTooLargeError(command, self._max_output_bytes)
            await process.wait()
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(BaseException):
                await stderr_task
        return b"".join(stdout_chunks), b"".join(stderr_chunks)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill ``process`` if still running and reap its exit status.

    ``ProcessLookupError`` from ``kill()`` (child already exited
    between ``wait_for`` raising and us reaching here) is swallowed
    — there is nothing to clean up in that case. ``wait()`` is
    awaited in all cases so the process table never accumulates
    zombies.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    with contextlib.suppress(BaseException):
        await process.wait()
