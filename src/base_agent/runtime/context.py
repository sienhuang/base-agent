"""Mutable state isolated to a single runtime execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from base_agent.models import (
    Artifact,
    Attachment,
    ExecutionPlan,
    MemoryMatch,
    Message,
    ModelResponse,
    PendingInput,
    TokenUsage,
)
from base_agent.profiles import AgentProfile
from base_agent.resources import ResourceFailure
from base_agent.runtime.state_machine import ExecutionState, RuntimeStateMachine
from base_agent.skills import Skill


@dataclass(slots=True)
class RuntimeContext:
    """All transient state for one run; a context is never shared between runs."""

    profile: AgentProfile
    messages: list[Message]
    skills: tuple[Skill, ...] = ()
    enabled_tool_names: tuple[str, ...] = ()
    run_id: UUID = field(default_factory=uuid4)
    state_machine: RuntimeStateMachine = field(default_factory=RuntimeStateMachine)
    step_count: int = 0
    tool_call_count: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    responses: list[ModelResponse] = field(default_factory=list)
    output: str | None = None
    error: str | None = None
    provider_name: str | None = None
    supervision_data: dict[str, Any] = field(default_factory=dict)
    pending_input: PendingInput | None = None
    plan: ExecutionPlan | None = None
    resource_failures: list[ResourceFailure] = field(default_factory=list)
    attachments: tuple[Attachment, ...] = ()
    artifacts: list[Artifact] = field(default_factory=list)
    input_text: str = ""
    memories: tuple[MemoryMatch, ...] = ()
    memory_initialized: bool = False
    memory_error: str | None = None

    @property
    def state(self) -> ExecutionState:
        return self.state_machine.state
