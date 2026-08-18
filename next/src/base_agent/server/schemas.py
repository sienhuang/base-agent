"""Request and response models for the optional Agent and Flow server."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from base_agent.flows import FlowStatus
from base_agent.models import RunStatus


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    stream: bool = False

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class StartRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus


class ArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str
    location: str | None = None
    media_type: str
    size_bytes: int | None = None
    sha256: str | None = None
    created_at: datetime | None = None
    producer_run_id: str | None = None
    producer_agent_id: str | None = None
    flow_run_id: str | None = None
    stage: str | None = None
    tool_call_id: str | None = None
    source_artifact_ids: tuple[str, ...] = ()


ArtifactRefResponse = ArtifactResponse


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    event_count: int
    output: Any | None = None
    text: str | None = None
    error: str | None = None
    artifacts: tuple[ArtifactResponse, ...] = ()


class EventResponse(BaseModel):
    """JSON/SSE representation of one persisted Run Event."""

    model_config = ConfigDict(extra="forbid")

    sequence: int
    kind: str
    run_id: str
    turn: int | None
    payload: dict[str, Any]


class StartFlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str
    stream: bool = False

    @field_validator("prompt")
    @classmethod
    def prompt_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value


class StartFlowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_run_id: str
    status: FlowStatus


class FlowChildRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    phase: str
    agent_run_id: str
    status: RunStatus
    source_plan_revision: int | None
    target_plan_revision: int | None
    step_id: str | None
    attempt: int | None


class FlowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flow_run_id: str
    plan_id: str | None = None
    status: FlowStatus
    event_count: int
    output: str | None = None
    error: str | None = None
    plan: dict[str, Any] | None = None
    child_runs: tuple[FlowChildRunResponse, ...] = ()
    artifacts: tuple[ArtifactResponse, ...] = ()
