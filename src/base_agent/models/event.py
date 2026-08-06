"""Immutable events emitted while advancing a Run."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.run import utc_now


class EventType(StrEnum):
    FLOW_CREATED = "flow.created"
    FLOW_STARTED = "flow.started"
    FLOW_RESUMED = "flow.resumed"
    FLOW_WAITING = "flow.waiting"
    FLOW_COMPLETED = "flow.completed"
    FLOW_FAILED = "flow.failed"
    FLOW_CANCELLED = "flow.cancelled"
    FLOW_INTERRUPTED = "flow.interrupted"
    FLOW_LIMIT_REACHED = "flow.limit_reached"
    AGENT_INVOCATION_STARTED = "agent_invocation.started"
    AGENT_INVOCATION_RESUMED = "agent_invocation.resumed"
    AGENT_INVOCATION_WAITING = "agent_invocation.waiting"
    AGENT_INVOCATION_COMPLETED = "agent_invocation.completed"
    AGENT_INVOCATION_FAILED = "agent_invocation.failed"
    AGENT_INVOCATION_CANCELLED = "agent_invocation.cancelled"
    AGENT_INVOCATION_INTERRUPTED = "agent_invocation.interrupted"
    AGENT_INVOCATION_LIMIT_REACHED = "agent_invocation.limit_reached"
    AGENT_INVOCATION_CANCELLATION_PROPAGATION_FAILED = (
        "agent_invocation.cancellation_propagation_failed"
    )
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
    TOOL_CONFIRMATION_REQUESTED = "tool_confirmation.requested"
    TOOL_CONFIRMATION_DECIDED = "tool_confirmation.decided"
    INPUT_RECEIVED = "input.received"
    PLAN_CREATED = "plan.created"
    PLAN_REVIEWED = "plan.reviewed"
    PLAN_UPDATED = "plan.updated"
    STEP_STARTED = "step.started"
    STEP_RESUMED = "step.resumed"
    STEP_WAITING = "step.waiting"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    STEP_CANCELLED = "step.cancelled"
    STEP_SKIPPED = "step.skipped"
    REACT_ITERATION_STARTED = "react.iteration.started"
    REACT_ACTION_BATCH_SELECTED = "react.action_batch.selected"
    REACT_OBSERVATION_BATCH_RECORDED = "react.observation_batch.recorded"
    REACT_ITERATION_COMPLETED = "react.iteration.completed"
    RESOURCE_ACQUIRED = "resource.acquired"
    RESOURCE_RELEASED = "resource.released"
    RESOURCE_FAILED = "resource.failed"
    ATTACHMENT_ADDED = "attachment.added"
    ARTIFACT_CREATED = "artifact.created"
    MEMORY_RETRIEVED = "memory.retrieved"
    MEMORY_FAILED = "memory.failed"
    SUPERVISOR_INTERVENED = "supervisor.intervened"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_INTERRUPTED = "run.interrupted"
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
