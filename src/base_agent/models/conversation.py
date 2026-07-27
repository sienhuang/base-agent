"""Conversation aggregates whose Turns are executed by normal Runs."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.run import RunStatus, utc_now


class Conversation(BaseModel):
    """Durable identity and concurrency state for an ordered multi-Run conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    profile_id: str = Field(min_length=1)
    version: int = Field(default=0, ge=0)
    active_run_id: UUID | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConversationTurn(BaseModel):
    """One user Turn whose execution identity is its Run ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    sequence: int = Field(ge=1)
    run_id: UUID
    status: RunStatus = RunStatus.RUNNING
    user_message: str = Field(min_length=1)
    assistant_message: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

