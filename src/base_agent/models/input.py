"""Provider-neutral human-input and confirmation models."""

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.run import utc_now


class ToolConfirmationDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ToolConfirmation(BaseModel):
    """Authenticated host decision bound to one exact confirmation request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    decision: ToolConfirmationDecision
    subject_id: str = Field(min_length=1, max_length=256)
    reason_code: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    decided_at: datetime = Field(default_factory=utc_now)


class WaitForInput(BaseModel):
    """A Tool outcome asking the Runtime to suspend until a human responds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PendingInput(BaseModel):
    """The exact Tool call that must be completed when a Run resumes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
