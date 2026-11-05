"""Structured JSON logging with secret redaction."""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

CONTEXT_FIELDS = (
    "incident_id",
    "action_id",
    "resource_type",
    "resource_name",
    "namespace",
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
        "***PRIVATE KEY REDACTED***",
    ),
    (
        re.compile(
            r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)[A-Za-z0-9._~+/=-]+"
        ),
        r"\1***",
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), r"\1***"),
    (
        re.compile(
            r"(?i)((?:password|passwd|secret|token|api[_-]?key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"
        ),
        r"\1***",
    ),
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^:/@\s]+):[^@/\s]+@"), r"\1:***@"),
)


def redact(text: str) -> str:
    """Mask tokens, passwords, URL credentials and private keys in free text."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "component": record.name.removeprefix("kubidoor."),
            "message": redact(record.getMessage()),
        }
        for field in CONTEXT_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = redact(str(value))
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Route uvicorn's loggers through the same JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True


def get_logger(component: str) -> logging.Logger:
    return logging.getLogger(f"kubidoor.{component}")


def log_context(**fields: Any) -> dict[str, Any]:
    """Build the ``extra=`` dict for structured context fields (None values dropped)."""
    return {k: v for k, v in fields.items() if k in CONTEXT_FIELDS and v is not None}
