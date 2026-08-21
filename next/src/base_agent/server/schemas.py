"""Request and response models for the optional Agent and Flow server."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from base_agent.execution import RecoveryDecision, RecoveryStrategy
from base_agent.flows import FlowStatus
from base_agent.governance import ToolConfirmationDecision
from base_agent.models import PendingInputKind, RunStatus


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


class ResumeRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: str

    @field_validator("input")
    @classmethod
    def input_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("input must not be blank")
        return value


class ConfirmRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    decision: ToolConfirmationDecision
    subject_id: str
    reason_code: str


class PendingInputResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    kind: PendingInputKind
    tool_call_id: str
    tool_name: str
    prompt: str
    metadata: dict[str, Any] = Field(default_factory=dict)


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


class RecoveryAttemptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt_id: str
    status: str
    owner_id: str
    runtime_build_id: str
    heartbeat_at: datetime
    lease_expires_at: datetime
    reason_code: str | None = None


class RecoveryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str
    revision: int
    detected_at: datetime
    last_attempt: RecoveryAttemptResponse | None = None


class RecoveryDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: RecoveryDecision
    strategy: RecoveryStrategy | None = None
    expected_revision: int = Field(ge=1)
    subject_id: str = Field(min_length=1, max_length=256)
    reason_code: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    idempotency_key: str = Field(min_length=1, max_length=256)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    event_count: int
    output: Any | None = None
    text: str | None = None
    error: str | None = None
    artifacts: tuple[ArtifactResponse, ...] = ()
    recovery: RecoveryResponse | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    pending_input: PendingInputResponse | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


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
    pending_input: PendingInputResponse | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    waiting_child_run_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
