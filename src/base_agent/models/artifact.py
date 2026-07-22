"""Immutable references to input attachments and generated artifacts."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Attachment(BaseModel):
    """A stored input made available to one or more Runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Artifact(BaseModel):
    """A stored output produced by one Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)
    checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=_utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
