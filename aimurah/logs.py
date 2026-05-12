"""Structured logging for AIMurahV3."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

from .config import LOG_PATH, REQUEST_LOG_PATH, ensure_data_dir

_logger: logging.Logger | None = None


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _log_level_from_env() -> int:
    raw = (os.environ.get("AIMURAH_LOG_LEVEL") or "INFO").upper().strip()
    return getattr(logging, raw, logging.INFO)


def get_logger() -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger
    ensure_data_dir()
    logger = logging.getLogger("aimurahv3")
    logger.setLevel(_log_level_from_env())
    logger.propagate = False

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    try:
        # Rotate the main log to keep disk use bounded on long-running VPSes.
        # Defaults: 5MB per file, 3 backups (total ~20MB). Tunable via env.
        max_bytes = _env_int("AIMURAH_LOG_MAX_BYTES", 5_000_000)
        backup_count = _env_int("AIMURAH_LOG_BACKUP_COUNT", 3)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        pass

    _logger = logger
    return logger


def _rotate_request_log_if_needed() -> None:
    """Cheap size-based rotation for the JSONL request log."""
    try:
        max_bytes = _env_int("AIMURAH_REQUEST_LOG_MAX_BYTES", 10_000_000)
        path = Path(REQUEST_LOG_PATH)
        if path.exists() and path.stat().st_size >= max_bytes:
            backup = path.with_suffix(path.suffix + ".1")
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            path.replace(backup)
    except OSError:
        pass


def log_request(entry: dict[str, Any]) -> None:
    """Append a structured request log line (JSONL)."""
    ensure_data_dir()
    _rotate_request_log_if_needed()
    entry = dict(entry)
    entry.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        with Path(REQUEST_LOG_PATH).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass
