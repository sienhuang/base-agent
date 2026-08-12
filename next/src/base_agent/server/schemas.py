"""Request and response models for the optional Run server."""

from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

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


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    event_count: int
    output: Any | None = None
    text: str | None = None
    error: str | None = None


class EventResponse(BaseModel):
    """JSON/SSE representation of one persisted Run Event."""

    model_config = ConfigDict(extra="forbid")

    sequence: int
    kind: str
    run_id: str
    payload: dict[str, Any]
