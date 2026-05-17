"""Structlog-backed module logger shared across the extractor.

Mirrors the import surface of ``src.app.core.logging.logger`` in
exodos-backend so vendored files can keep their original
``from ...logger import logger`` import line unchanged.
"""

import structlog

logger = structlog.get_logger("harvester.extractor")
