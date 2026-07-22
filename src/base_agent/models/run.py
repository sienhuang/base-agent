"""Durable aggregate describing one agent execution."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.artifact import Artifact, Attachment
from base_agent.models.model import TokenUsage
from base_agent.models.skill import SkillReference


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"
    WAITING = "waiting"


def utc_now() -> datetime:
    return datetime.now(UTC)


class Run(BaseModel):
    """Current durable snapshot; detailed history remains in ordered events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    profile_id: str
    status: RunStatus = RunStatus.CREATED
    step_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    skills: tuple[SkillReference, ...] = ()
    output: str | None = None
    error: str | None = None
    cancel_requested: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
