"""HTTP request and response models for the optional Run server."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models import ExecutionPlan, RunStatus


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    skills: tuple[str, ...] = ()
    attachment_ids: tuple[UUID, ...] = ()
    plan: ExecutionPlan | None = None


class StartRunResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: RunStatus


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str = Field(min_length=1)
