"""Fail-closed confirmation contracts for side-effecting Tool calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class SideEffect(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


class Confirmation(StrEnum):
    NONE = "none"
    REQUIRED = "required"


class ConfirmationPolicyDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    DEFER = "defer"


class ToolConfirmationDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class ToolConfirmation(BaseModel):
    """Authenticated host decision bound to one exact pending request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    decision: ToolConfirmationDecision
    subject_id: str = Field(min_length=1, max_length=256)
    reason_code: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )
    decided_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    run_id: str
    agent_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    side_effect: SideEffect
    request_id: str = ""
    tool_call_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id:
            object.__setattr__(self, "request_id", str(uuid4()))


@runtime_checkable
class ConfirmationPolicy(Protocol):
    async def confirm(
        self,
        request: ConfirmationRequest,
    ) -> bool | ConfirmationPolicyDecision: ...


class DenyConfirmations:
    async def confirm(self, request: ConfirmationRequest) -> bool:
        return False


class AllowAllConfirmations:
    """Test helper; production applications should ask an authenticated user."""

    async def confirm(self, request: ConfirmationRequest) -> bool:
        return True


class AwaitHumanConfirmations:
    """Suspend required confirmations for an authenticated host decision."""

    async def confirm(self, request: ConfirmationRequest) -> ConfirmationPolicyDecision:
        return ConfirmationPolicyDecision.DEFER
