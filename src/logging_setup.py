"""Structured logging to logs/agent.log (rotating ~10MB) + stdout for cron.log."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from . import settings


def setup_logging(verbose: bool = False) -> logging.Logger:
    settings.ensure_dirs()
    logger = logging.getLogger("agent")
    if logger.handlers:
        return logger  # already configured
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = RotatingFileHandler(
        settings.LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    return logger
