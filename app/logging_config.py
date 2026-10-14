"""Structured logging: JSON (default) or human-readable text, with per-review context fields."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Context propagated through asyncio tasks so every log line in a review carries its IDs.
log_context: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "log_context", default=None
)

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


def bind_log_context(**fields: Any) -> contextvars.Token[dict[str, Any] | None]:
    """Add fields (e.g. ``delivery_id``, ``review_id``) to the current context's log lines."""
    current = dict(log_context.get() or {})
    current.update({k: v for k, v in fields.items() if v is not None})
    return log_context.set(current)


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
    return {**(log_context.get() or {}), **extras}


class JsonFormatter(logging.Formatter):
    """Render each record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    """Human-readable format with context fields appended as ``key=value``."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = _extra_fields(record)
        if extras:
            base += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        return base


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install a single stdout handler on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    # httpx logs every request at INFO, which would include GitHub URLs on every call.
    logging.getLogger("httpx").setLevel(logging.WARNING)
