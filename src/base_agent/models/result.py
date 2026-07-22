"""Terminal result returned by the public Agent facade."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.message import Message
from base_agent.models.model import TokenUsage


class AgentResultStatus(StrEnum):
    """Stable execution outcomes shared by local and server runtimes."""

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    LIMIT_REACHED = "limit_reached"
    WAITING = "waiting"


class AgentResult(BaseModel):
    """Provider-independent result of one agent run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AgentResultStatus
    output: str | None = None
    messages: tuple[Message, ...] = ()
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
