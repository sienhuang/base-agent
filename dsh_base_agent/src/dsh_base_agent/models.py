"""Framework-owned values shared by Agent, Flow, Tool and HTTP layers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class ToolChoice(StrEnum):
    NONE = "none"
    AUTO = "auto"
    REQUIRED = "required"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("ToolSpec name must not be blank")
        object.__setattr__(self, "input_schema", MappingProxyType(dict(self.input_schema)))


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int
    kind: str
    run_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    turn: int | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("event sequence must be positive")
        if not self.kind.strip() or not self.run_id.strip():
            raise ValueError("event kind and run_id must not be blank")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


type Observer = Callable[[Event], None | Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SkillSnapshot:
    name: str
    version: str
    description: str = ""
    package_fingerprint: str | None = None
    required_tools: tuple[str, ...] = ()
    optional_tools: tuple[str, ...] = ()
    provided_tools: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ()
    optional_permissions: tuple[str, ...] = ()
    scripts: tuple[str, ...] = ()
    script_runtimes: tuple[str, ...] = ()
    script_permission: str | None = None
    script_timeout_seconds: float | None = None
    script_max_output_bytes: int | None = None
    references: tuple[str, ...] = ()
    templates: tuple[str, ...] = ()
    assets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HarnessSnapshot:
    """Immutable description of the exact business Agent sent to DSH."""

    agent_id: str
    agent_version: str
    system_prompt: str
    model_name: str
    provider_name: str
    tool_specs: tuple[ToolSpec, ...]
    skills: tuple[SkillSnapshot, ...]
    permissions: frozenset[str]
    max_model_turns: int
    max_tool_calls_per_turn: int
    max_tool_observation_bytes: int
    max_run_observation_bytes: int
    context_policy: str
    context_window_tokens: int | None
    context_reserved_output_tokens: int
    context_safety_margin_tokens: int
    system_context_block_ids: tuple[str, ...]
    required_system_context_block_ids: tuple[str, ...]
    memory_provider: str
    memory_writer: str
    memory_recall_limit: int
    memory_recall_timeout_seconds: float
    fingerprint: str

    @property
    def skill_names(self) -> tuple[str, ...]:
        return tuple(skill.name for skill in self.skills)


@dataclass(frozen=True, slots=True)
class RunFailure:
    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class RunInvocation:
    """Correlation supplied by an outer Flow for one child Agent Run."""

    flow_run_id: str | None = None
    invocation_id: str | None = None
    stage: str | None = None
    step_id: str | None = None
    attempt: int | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.flow_run_id, "flow_run_id"),
            (self.invocation_id, "invocation_id"),
            (self.stage, "stage"),
            (self.step_id, "step_id"),
        ):
            if value is not None and not value.strip():
                raise ValueError(f"{name} must not be blank when provided")
        if self.attempt is not None and self.attempt < 1:
            raise ValueError("attempt must be positive when provided")


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: str
    status: RunStatus
    output: Any
    text: str
    snapshot: HarnessSnapshot
    events: tuple[Event, ...]
    error: str | None = None
    failure: RunFailure | None = None


__all__ = [
    "Event",
    "HarnessSnapshot",
    "Observer",
    "RunFailure",
    "RunInvocation",
    "RunResult",
    "RunStatus",
    "SkillSnapshot",
    "ToolChoice",
    "ToolSpec",
]
