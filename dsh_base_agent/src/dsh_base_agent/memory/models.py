"""Provider-neutral Memory capability models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _MemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Explicit application-owned tenant/namespace path."""

    namespace: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.namespace or any(not part.strip() for part in self.namespace):
            raise ValueError("memory scope namespace parts must not be blank")


class RecallQuery(_MemoryModel):
    tenant: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    text: str = Field(min_length=1)
    subject: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=8, ge=1, le=100)


class MemoryCandidate(_MemoryModel):
    ref: str = Field(min_length=1)
    tenant: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    content: str = Field(min_length=1)
    score: float = Field(ge=0, le=1)
    source: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryWrite(_MemoryModel):
    tenant: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_event_ref: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    subject: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryRef(_MemoryModel):
    ref: str = Field(min_length=1)
    tenant: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
