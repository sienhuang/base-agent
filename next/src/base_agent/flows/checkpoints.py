"""Strict JSON helpers for durable built-in Flow strategy checkpoints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, TypeAdapter

from base_agent.artifacts import Artifact
from base_agent.flow_store import FlowCheckpoint

CheckpointWriter = Callable[[FlowCheckpoint], Awaitable[FlowCheckpoint]]

def strict_json_value(value: Any) -> Any:
    """Convert nested Pydantic/stdlib dataclasses into finite mutable JSON."""
    normalized = _jsonable(value)
    try:
        return json.loads(
            json.dumps(
                normalized,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Flow checkpoint state must contain only finite JSON") from exc


def restore_dataclass[DataclassT](
    type_: type[DataclassT],
    payload: Any,
) -> DataclassT:
    """Validate one JSON snapshot back into a frozen public dataclass graph."""
    adapter = TypeAdapter(type_)
    adapter.rebuild(_types_namespace={"Artifact": Artifact})
    return adapter.validate_python(payload)


def definition_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        strict_json_value(payload),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def callable_identity(value: Callable[..., Any] | None) -> str | None:
    if value is None:
        return None
    module = getattr(value, "__module__", type(value).__module__)
    qualname = getattr(value, "__qualname__", type(value).__qualname__)
    return f"{module}:{qualname}"


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    raise TypeError(f"unsupported Flow checkpoint value '{type(value).__name__}'")


__all__ = [
    "CheckpointWriter",
    "callable_identity",
    "definition_fingerprint",
    "restore_dataclass",
    "strict_json_value",
]
