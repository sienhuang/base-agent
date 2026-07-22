"""Immutable events emitted while advancing a Run."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.run import utc_now


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_RESUMED = "run.resumed"
    SKILL_SELECTED = "skill.selected"
    SKILL_LOADED = "skill.loaded"
    MODEL_REQUESTED = "model.requested"
    MODEL_RESPONDED = "model.responded"
    TOOL_REQUESTED = "tool.requested"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_WAITING = "tool.waiting"
    INPUT_RECEIVED = "input.received"
    SUPERVISOR_INTERVENED = "supervisor.intervened"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_LIMIT_REACHED = "run.limit_reached"
    RUN_WAITING = "run.waiting"


class RuntimeEvent(BaseModel):
    """One ordered fact in the history of a Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    sequence: int = Field(ge=1)
    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    data: dict[str, Any] = Field(default_factory=dict)
