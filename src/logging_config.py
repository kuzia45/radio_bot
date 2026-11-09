"""Readable console and airplay logging configuration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

MOSCOW_TZ = timezone(timedelta(hours=3), name="Europe/Moscow")


class MoscowConsoleFormatter(logging.Formatter):
    """Format compact log lines using Moscow time."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, MOSCOW_TZ)
        return moment.strftime(datefmt or "%d.%m.%Y %H:%M:%S")


def configure_logging(log_dir: Path, airplay_logger: logging.Logger) -> None:
    """Configure a readable console and bounded radio-history file."""
    console = logging.StreamHandler()
    console.setFormatter(
        MoscowConsoleFormatter(
            "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
        )
    )
    logging.basicConfig(level=logging.INFO, handlers=[console], force=True)

    log_dir.mkdir(parents=True, exist_ok=True)
    airplay_handler = RotatingFileHandler(
        log_dir / "airplay.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    airplay_handler.setFormatter(
        MoscowConsoleFormatter("%(asctime)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    airplay_logger.handlers.clear()
    airplay_logger.addHandler(airplay_handler)
    airplay_logger.setLevel(logging.INFO)
    airplay_logger.propagate = False

    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
