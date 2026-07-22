"""Serializable snapshot used to resume a suspended RuntimeContext."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.models import (
    Artifact,
    Attachment,
    ExecutionPlan,
    MemoryMatch,
    Message,
    ModelResponse,
    PendingInput,
    RunStatus,
    TokenUsage,
)
from base_agent.profiles import AgentProfile
from base_agent.resources import ResourceFailure
from base_agent.skills import Skill

if TYPE_CHECKING:
    from base_agent.runtime.context import RuntimeContext


class RuntimeCheckpoint(BaseModel):
    """Everything needed to continue the same Run after a waiting boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    profile: AgentProfile
    messages: tuple[Message, ...]
    skills: tuple[Skill, ...] = ()
    enabled_tool_names: tuple[str, ...] = ()
    state: RunStatus = RunStatus.WAITING
    step_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    responses: tuple[ModelResponse, ...] = ()
    output: str | None = None
    error: str | None = None
    provider_name: str | None = None
    supervision_data: dict[str, Any] = Field(default_factory=dict)
    pending_input: PendingInput
    plan: ExecutionPlan | None = None
    resource_failures: tuple[ResourceFailure, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    input_text: str
    memories: tuple[MemoryMatch, ...] = ()
    memory_initialized: bool = False
    memory_error: str | None = None

    @model_validator(mode="after")
    def validate_waiting_state(self) -> RuntimeCheckpoint:
        if self.state is not RunStatus.WAITING:
            raise ValueError("runtime checkpoints must represent a waiting Run")
        return self

    @classmethod
    def from_context(cls, context: RuntimeContext) -> RuntimeCheckpoint:
        if context.state is not RunStatus.WAITING or context.pending_input is None:
            raise ValueError("only a waiting context with pending input can be checkpointed")
        return cls(
            run_id=context.run_id,
            profile=context.profile,
            messages=tuple(context.messages),
            skills=context.skills,
            enabled_tool_names=context.enabled_tool_names,
            state=context.state,
            step_count=context.step_count,
            tool_call_count=context.tool_call_count,
            usage=context.usage,
            responses=tuple(context.responses),
            output=context.output,
            error=context.error,
            provider_name=context.provider_name,
            supervision_data=context.supervision_data,
            pending_input=context.pending_input,
            plan=context.plan,
            resource_failures=tuple(context.resource_failures),
            attachments=context.attachments,
            artifacts=tuple(context.artifacts),
            input_text=context.input_text,
            memories=context.memories,
            memory_initialized=context.memory_initialized,
            memory_error=context.memory_error,
        )

    def restore(self) -> RuntimeContext:
        from base_agent.runtime.context import RuntimeContext
        from base_agent.runtime.state_machine import RuntimeStateMachine

        return RuntimeContext(
            profile=self.profile,
            messages=list(self.messages),
            skills=self.skills,
            enabled_tool_names=self.enabled_tool_names,
            run_id=self.run_id,
            state_machine=RuntimeStateMachine(self.state),
            step_count=self.step_count,
            tool_call_count=self.tool_call_count,
            usage=self.usage,
            responses=list(self.responses),
            output=self.output,
            error=self.error,
            provider_name=self.provider_name,
            supervision_data=dict(self.supervision_data),
            pending_input=self.pending_input,
            plan=self.plan,
            resource_failures=list(self.resource_failures),
            attachments=self.attachments,
            artifacts=list(self.artifacts),
            input_text=self.input_text,
            memories=self.memories,
            memory_initialized=self.memory_initialized,
            memory_error=self.memory_error,
        )
