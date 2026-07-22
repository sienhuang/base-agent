"""Provider-neutral models for optional memory retrieval."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _utc_now() -> datetime:
    return datetime.now(UTC)


class MemoryFailureMode(StrEnum):
    BEST_EFFORT = "best_effort"
    REQUIRED = "required"


class MemoryRecord(BaseModel):
    """One immutable piece of retrievable text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    content: str = Field(min_length=1)
    namespace: str = Field(default="default", min_length=1, max_length=128)
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("content", "namespace")
    @classmethod
    def reject_blank_values(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory values must not be blank")
        return value


class MemoryQuery(BaseModel):
    """A bounded search request independent of a vector database or model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=100)
    namespace: str | None = Field(default=None, min_length=1, max_length=128)
    profile_id: str | None = None
    run_id: UUID | None = None
    filters: dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory query text must not be blank")
        return value


class MemoryMatch(BaseModel):
    """A retrieved record and its normalized relevance score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    record: MemoryRecord
    score: float = Field(ge=0, le=1)
