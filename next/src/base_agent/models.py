"""Provider-neutral values shared by the small public SDK and its Harness."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from base_agent.verification import VerificationEvidence


class ToolChoice(StrEnum):
    NONE = "none"
    AUTO = "auto"
    REQUIRED = "required"


class FinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


class RunStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    NOT_FOUND = "not_found"
    DENIED = "denied"
    LIMIT_EXCEEDED = "limit_exceeded"
    INVALID_ARGUMENTS = "invalid_arguments"
    CONFIRMATION_DENIED = "confirmation_denied"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role="user", content=content)

    @classmethod
    def assistant(
        cls,
        content: str,
        *,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> Message:
        return cls(role="assistant", content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, call: ToolCall, content: str) -> Message:
        return cls(
            role="tool",
            content=content,
            tool_call_id=call.id,
            name=call.name,
        )


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.total_tokens,
            self.cached_input_tokens,
            self.reasoning_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("token usage values must be non-negative")
        expected_total = self.input_tokens + self.output_tokens
        if self.total_tokens == 0:
            object.__setattr__(self, "total_tokens", expected_total)
        elif self.total_tokens != expected_total:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")


@dataclass(frozen=True, slots=True)
class ModelRequest:
    model: str
    system_prompt: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...]
    tool_choice: ToolChoice
    response_schema: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: FinishReason = FinishReason.STOP
    usage: TokenUsage = field(default_factory=TokenUsage)
    provider_data: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_data",
            MappingProxyType(dict(self.provider_data)),
        )


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("model text delta must not be empty")


@dataclass(frozen=True, slots=True)
class ModelToolCallDelta:
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("model ToolCall delta index must be non-negative")
        if self.id is None and self.name is None and not self.arguments_delta:
            raise ValueError("model ToolCall delta must contain data")


@dataclass(frozen=True, slots=True)
class ModelStreamCompleted:
    response: ModelResponse


type ModelStreamEvent = ModelTextDelta | ModelToolCallDelta | ModelStreamCompleted


@dataclass(frozen=True, slots=True)
class ToolResult:
    call: ToolCall
    status: ToolResultStatus
    data: Any = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    sequence: int
    kind: str
    run_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


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
class RunResult:
    run_id: str
    status: RunStatus
    output: Any
    text: str
    snapshot: HarnessSnapshot
    events: tuple[Event, ...]
    tool_results: tuple[ToolResult, ...] = ()
    evidence: tuple[VerificationEvidence, ...] = ()
    error: str | None = None
    failure: RunFailure | None = None
