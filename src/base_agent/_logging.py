"""Internal structured logging support for base-agent."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

_LOGGER_NAME = "base_agent"
_UNSET = object()
_DEFAULT_RETENTION_DAYS = 30
_handler_lock = threading.Lock()
_active_handler: TimedRotatingFileHandler | None = None
_active_path: Path | None = None
_active_retention_days: int | None = None

_request_id: ContextVar[str | None] = ContextVar("base_agent_request_id", default=None)
_conversation_id: ContextVar[str | None] = ContextVar(
    "base_agent_conversation_id", default=None
)
_run_id: ContextVar[str | None] = ContextVar("base_agent_run_id", default=None)
_turn_sequence: ContextVar[int | None] = ContextVar(
    "base_agent_turn_sequence", default=None
)

_STANDARD_RECORD_FIELDS = frozenset(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__
)
_SECRET_KEY = re.compile(
    r"(^|[_-])(authorization|api[_-]?key|access[_-]?key|secret|password|passwd|"
    r"access[_-]?token|refresh[_-]?token|bearer[_-]?token|token|credential)([_-]|$)",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(authorization|api[_-]?key|access[_-]?key|secret|password|passwd|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_OPENAI_STYLE_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")


def configure_file_logging(
    path: str | Path | None = None,
    *,
    level: str | int | None = None,
    retention_days: int = _DEFAULT_RETENTION_DAYS,
) -> Path:
    """Install one package-scoped rotating file handler and return its path."""

    global _active_handler, _active_path, _active_retention_days

    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    configured = path if path is not None else os.getenv("BASE_AGENT_LOG_FILE")
    desired_path = (
        Path(configured).expanduser()
        if configured is not None and str(configured).strip()
        else Path.cwd() / "logs" / "base-agent.log"
    )
    desired_path = desired_path.resolve()
    configured_level = _log_level(level)

    with _handler_lock:
        if (
            _active_handler is not None
            and _active_path == desired_path
            and _active_retention_days == retention_days
        ):
            logging.getLogger(_LOGGER_NAME).setLevel(configured_level)
            return desired_path

        try:
            desired_path.parent.mkdir(parents=True, exist_ok=True)
            handler = _build_handler(desired_path, retention_days)
        except OSError:
            fallback = Path(tempfile.gettempdir()) / "base-agent" / "base-agent.log"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            desired_path = fallback.resolve()
            handler = _build_handler(desired_path, retention_days)

        package_logger = logging.getLogger(_LOGGER_NAME)
        if _active_handler is not None:
            package_logger.removeHandler(_active_handler)
            _active_handler.close()
        package_logger.addHandler(handler)
        package_logger.setLevel(configured_level)
        package_logger.propagate = False
        _active_handler = handler
        _active_path = desired_path
        _active_retention_days = retention_days
        return desired_path


def set_log_context(
    *,
    request_id: Any = _UNSET,
    conversation_id: Any = _UNSET,
    run_id: Any = _UNSET,
    turn_sequence: Any = _UNSET,
) -> tuple[Token[Any], ...]:
    """Bind correlation values to the current async context."""

    return (
        _request_id.set(
            _request_id.get()
            if request_id is _UNSET
            else (str(request_id) if request_id is not None else None)
        ),
        _conversation_id.set(
            _conversation_id.get()
            if conversation_id is _UNSET
            else (str(conversation_id) if conversation_id is not None else None)
        ),
        _run_id.set(
            _run_id.get()
            if run_id is _UNSET
            else (str(run_id) if run_id is not None else None)
        ),
        _turn_sequence.set(
            _turn_sequence.get()
            if turn_sequence is _UNSET
            else (int(turn_sequence) if turn_sequence is not None else None)
        ),
    )


def reset_log_context(tokens: tuple[Token[Any], ...]) -> None:
    """Restore correlation values previously returned by ``set_log_context``."""

    _turn_sequence.reset(tokens[3])
    _run_id.reset(tokens[2])
    _conversation_id.reset(tokens[1])
    _request_id.reset(tokens[0])


def _build_handler(path: Path, retention_days: int) -> TimedRotatingFileHandler:
    handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
        utc=False,
    )
    handler.setFormatter(_JsonFormatter())
    return handler


def _log_level(configured: str | int | None) -> int:
    if isinstance(configured, int):
        return configured
    value = configured or os.environ.get("BASE_AGENT_LOG_LEVEL", "INFO")
    resolved = logging.getLevelName(value.strip().upper())
    return resolved if isinstance(resolved, int) else logging.INFO


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "process_id": record.process,
            "message": _redact(record.getMessage()),
            "request_id": _request_id.get(),
            "conversation_id": _conversation_id.get(),
            "run_id": _run_id.get(),
            "turn_sequence": _turn_sequence.get(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            payload[key] = _safe_value(key, value)
        if record.exc_info:
            payload["exception"] = _redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, default=str)


def _safe_value(key: str, value: Any) -> Any:
    if _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _safe_value(str(item_key), item) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_safe_value(key, item) for item in value]
    if isinstance(value, str):
        return _redact(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(str(value))


def _redact(value: str) -> str:
    value = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
    value = _AWS_ACCESS_KEY.sub("[REDACTED]", value)
    value = _BEARER_TOKEN.sub("Bearer [REDACTED]", value)
    return _OPENAI_STYLE_KEY.sub("[REDACTED]", value)
