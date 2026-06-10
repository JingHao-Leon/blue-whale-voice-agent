"""
Structured logger setup. We use loguru so we get nice colours in dev and
plain JSON in prod without re-implementing.
"""
from __future__ import annotations

import sys

from loguru import logger

from app.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log_level,
        colorize=True,
        format=(
            "<green>{time:HH:mm:ss.SSS}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}:{function}:{line}</cyan> - <level>{message}</level>"
        ),
    )
    # Quieter libraries.
    logger.disable("httpx")
    logger.disable("httpcore")


__all__ = ["logger", "setup_logging"]
