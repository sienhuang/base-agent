"""HTTP request and response models for the optional Run server."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models import ExecutionPlan, RunStatus


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    conversation_id: UUID | None = None
    skills: tuple[str, ...] = ()
    attachment_ids: tuple[UUID, ...] = ()
    plan: ExecutionPlan | None = None
    planning: bool = False


class StartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: RunStatus
    conversation_id: UUID | None = None
    turn_sequence: int | None = None


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1)


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metadata: dict[str, Any] = Field(default_factory=dict)
