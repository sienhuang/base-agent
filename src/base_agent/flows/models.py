"""Serializable definitions and handoff contracts for simple Agent Flows."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from base_agent.models import (
    AgentResultStatus,
    Artifact,
    Attachment,
    PendingInput,
    RunStatus,
    TokenUsage,
    ToolConfirmation,
)
from base_agent.profiles import AgentDefinition

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
_VERSION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._+-]*$"


class FlowAgent(BaseModel):
    """One stable key bound to an Agent definition inside a Flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    definition: AgentDefinition


class FlowBudget(BaseModel):
    """Versioned aggregate limits enforced across all Agent invocations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    max_invocations: int = Field(default=20, ge=1)
    max_input_tokens: int | None = Field(default=None, ge=1)
    max_output_tokens: int | None = Field(default=None, ge=1)
    max_total_tokens: int | None = Field(default=None, ge=1)
    max_model_calls: int | None = Field(default=None, ge=1)
    max_tool_calls: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)


class FlowDefinition(BaseModel):
    """Versioned declaration of named Agents and the strategy coordinating them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    version: str = Field(
        min_length=1,
        max_length=64,
        pattern=_VERSION_PATTERN,
    )
    agents: tuple[FlowAgent, ...] = Field(min_length=1)
    strategy: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    budget: FlowBudget = Field(default_factory=FlowBudget)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_max_invocations(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "max_invocations" not in data:
            return data
        normalized = dict(data)
        legacy_limit = normalized.pop("max_invocations")
        budget_value = normalized.get("budget")
        if budget_value is None:
            normalized["budget"] = {"max_invocations": legacy_limit}
            return normalized
        budget = (
            budget_value.model_dump(mode="python")
            if isinstance(budget_value, FlowBudget)
            else dict(budget_value)
        )
        configured = budget.get("max_invocations", 20)
        if configured != legacy_limit:
            raise ValueError(
                "max_invocations conflicts with budget.max_invocations"
            )
        budget["max_invocations"] = legacy_limit
        normalized["budget"] = budget
        return normalized

    @model_validator(mode="after")
    def validate_agent_keys(self) -> FlowDefinition:
        keys = [agent.key for agent in self.agents]
        if len(set(keys)) != len(keys):
            raise ValueError("Flow agent keys must be unique")
        return self

    def agent(self, key: str) -> AgentDefinition:
        """Resolve one named Agent without exposing a mutable registry."""
        for agent in self.agents:
            if agent.key == key:
                return agent.definition
        raise KeyError(f"Flow '{self.id}' has no Agent named '{key}'")

    @property
    def max_invocations(self) -> int:
        """Backward-compatible view of the canonical FlowBudget limit."""
        return self.budget.max_invocations

    @property
    def fingerprint(self) -> str:
        """Return a stable hash of the Flow definition and nested Agent definitions."""
        payload: dict[str, Any] = {
            "agents": [
                {
                    "definition_fingerprint": agent.definition.fingerprint,
                    "key": agent.key,
                }
                for agent in self.agents
            ],
            "id": self.id,
            "budget": self.budget.model_dump(mode="json"),
            "metadata": self.metadata,
            "strategy": self.strategy,
            "version": self.version,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


class AgentHandoff(BaseModel):
    """Explicit bounded context passed from one Agent invocation to another."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_agent_key: str = Field(
        min_length=1,
        max_length=128,
        pattern=_KEY_PATTERN,
    )
    summary: str = Field(min_length=1)
    data: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: tuple[Artifact, ...] = ()

    @model_validator(mode="after")
    def validate_artifacts(self) -> AgentHandoff:
        _require_unique_ids(self.artifacts, label="handoff Artifacts")
        return self


class AgentInvocationInput(BaseModel):
    """Input visible to one Agent invocation within a Flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=1)
    handoff: AgentHandoff | None = None
    attachments: tuple[Attachment, ...] = ()

    @model_validator(mode="after")
    def validate_attachments(self) -> AgentInvocationInput:
        _require_unique_ids(self.attachments, label="invocation Attachments")
        return self


class FlowInput(BaseModel):
    """Top-level user input explicitly shared with a Flow strategy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=1)
    attachments: tuple[Attachment, ...] = ()

    @model_validator(mode="after")
    def validate_attachments(self) -> FlowInput:
        _require_unique_ids(self.attachments, label="Flow input Attachments")
        return self


class AgentInvocationRequest(BaseModel):
    """One request from a Flow strategy to its AgentInvoker boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flow_run_id: UUID
    invocation_id: UUID = Field(default_factory=uuid4)
    sequence: int = Field(ge=1)
    agent_key: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    definition_id: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    definition_version: str = Field(
        min_length=1,
        max_length=64,
        pattern=_VERSION_PATTERN,
    )
    definition_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: AgentInvocationInput


class AgentInvocationResume(BaseModel):
    """Human input used to resume one waiting Agent invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flow_run_id: UUID
    invocation_id: UUID
    agent_key: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    user_input: str | None = Field(default=None, min_length=1)
    confirmation: ToolConfirmation | None = None

    @model_validator(mode="after")
    def validate_resume_payload(self) -> AgentInvocationResume:
        if (self.user_input is None) == (self.confirmation is None):
            raise ValueError(
                "AgentInvocationResume requires exactly one of "
                "user_input or confirmation"
            )
        return self


class AgentInvocationCancel(BaseModel):
    """Cancellation request for one active Agent invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flow_run_id: UUID
    invocation_id: UUID
    agent_key: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    reason: str = Field(min_length=1, max_length=1_000)


class AgentInvocationResult(BaseModel):
    """Bounded result returned to a Flow strategy by an AgentInvoker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    flow_run_id: UUID
    invocation_id: UUID
    agent_key: str = Field(min_length=1, max_length=128, pattern=_KEY_PATTERN)
    status: AgentResultStatus
    output: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    artifacts: tuple[Artifact, ...] = ()
    pending_input: PendingInput | None = None
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_status_payload(self) -> AgentInvocationResult:
        _require_unique_ids(self.artifacts, label="invocation result Artifacts")
        if self.status is AgentResultStatus.WAITING:
            if self.pending_input is None:
                raise ValueError("a waiting Agent invocation requires pending_input")
        elif self.pending_input is not None:
            raise ValueError("only a waiting Agent invocation may include pending_input")

        failed = {
            AgentResultStatus.FAILED,
            AgentResultStatus.CANCELLED,
            AgentResultStatus.INTERRUPTED,
            AgentResultStatus.LIMIT_REACHED,
        }
        if self.status in failed and not self.error:
            raise ValueError(
                f"an Agent invocation with status '{self.status.value}' requires an error"
            )
        if self.status is AgentResultStatus.COMPLETED and self.error is not None:
            raise ValueError("a completed Agent invocation cannot include an error")
        return self


class FlowResult(BaseModel):
    """Application-facing result of running or resuming a Flow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    status: RunStatus
    output: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    artifacts: tuple[Artifact, ...] = ()
    invocation_count: int = Field(ge=0)
    model_call_count: int = Field(default=0, ge=0)
    tool_call_count: int = Field(default=0, ge=0)
    waiting_invocation_id: UUID | None = None
    pending_input: PendingInput | None = None
    error: str | None = None

    @model_validator(mode="after")
    def validate_status_payload(self) -> FlowResult:
        waiting = self.status is RunStatus.WAITING
        if waiting and (
            self.waiting_invocation_id is None or self.pending_input is None
        ):
            raise ValueError(
                "a waiting Flow result requires an invocation id and pending input"
            )
        if not waiting and (
            self.waiting_invocation_id is not None or self.pending_input is not None
        ):
            raise ValueError("only a waiting Flow result may include pending input")
        failed = {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
            RunStatus.LIMIT_REACHED,
        }
        if self.status in failed and not self.error:
            raise ValueError(
                f"a Flow result with status '{self.status.value}' requires an error"
            )
        if self.status is RunStatus.COMPLETED and self.error is not None:
            raise ValueError("a completed Flow result cannot include an error")
        return self


def _require_unique_ids(items: tuple[Any, ...], *, label: str) -> None:
    identifiers = [item.id for item in items]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(f"{label} must be unique")
