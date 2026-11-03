"""Context candidates, decisions, and visibility scopes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from dsh_base_agent.memory import MemoryAccess


class VisibilityScope(StrEnum):
    REQUEST = "request"
    TURN = "turn"
    SESSION = "session"


class ContextSource(StrEnum):
    RUNTIME = "runtime"
    WORKSPACE = "workspace"
    APPLICATION = "application"


class ContextMessageRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_sequence: int = Field(ge=1)
    token_estimate: int = Field(ge=0)
    protocol_group: str | None = None


class ContextBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source: ContextSource
    trusted: bool = False
    token_estimate: int = Field(ge=0)


class ContextSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    ref: str = Field(min_length=1)
    scope: VisibilityScope


class ContextDecision(BaseModel):
    """Policy output. The Context gateway must durably commit this projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    history_event_sequences: tuple[int, ...] = ()
    context_blocks: tuple[ContextSelection, ...] = ()
    memories: tuple[ContextSelection, ...] = ()
    reason_code: str = Field(min_length=1)
    estimated_tokens: int = Field(ge=0)


class CommittedContextDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    policy_name: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    input_revision: int = Field(ge=0)
    digest: str = Field(min_length=64, max_length=64)
    decision: ContextDecision


@dataclass(frozen=True, slots=True)
class ContextPolicyContext:
    run_id: str
    session_id: str
    input_revision: int
    turn: int
    step: int
    tenant: str
    namespace: str
    current_input: str
    history: tuple[ContextMessageRef, ...]
    context_blocks: tuple[ContextBlock, ...]
    memory: MemoryAccess
    available_tokens: int
