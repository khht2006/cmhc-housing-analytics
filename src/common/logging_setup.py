"""
Structured run logging.

Every pipeline stage logs to console AND to a dated file under logs/. The
monthly refresh runs unattended via Task Scheduler, so when a month's numbers
look wrong the log is the only forensic record of what was pulled and when.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from src.common.paths import LOG_DIR

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)

        file_handler = logging.FileHandler(
            LOG_DIR / f"pipeline_{stamp}.log", encoding="utf-8"
        )
        file_handler.setFormatter(fmt)

        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.handlers = [console, file_handler]
        _CONFIGURED = True

    return logging.getLogger(name)
