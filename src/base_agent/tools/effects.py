"""Tool-side governance contracts for external side effects."""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models import (
    ToolCall,
)

if TYPE_CHECKING:
    from base_agent.tools.context import ToolContext


class ToolSideEffectMode(StrEnum):
    """Declared replay behavior for one Tool implementation."""

    UNSPECIFIED = "unspecified"
    READ_ONLY = "read_only"
    UNSAFE = "unsafe"
    IDEMPOTENT = "idempotent"


class ToolConfirmationMode(StrEnum):
    """Whether an execution requires an explicit typed decision."""

    NONE = "none"
    REQUIRED = "required"


class ToolConfirmationRequest(BaseModel):
    """Bounded approval request persisted in PendingInput metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1, max_length=128)
    side_effect_mode: ToolSideEffectMode
    prompt: str = Field(min_length=1, max_length=1_000)


class ToolConfirmationPolicy(Protocol):
    """Create a bounded request without executing the Tool."""

    async def request(
        self,
        call: ToolCall,
        *,
        tool_name: str,
        side_effect_mode: ToolSideEffectMode,
        confirmation_mode: ToolConfirmationMode,
        context: ToolContext,
    ) -> ToolConfirmationRequest | None: ...


class DeclaredToolConfirmationPolicy:
    """Require confirmation exactly when the Tool declaration requests it."""

    async def request(
        self,
        call: ToolCall,
        *,
        tool_name: str,
        side_effect_mode: ToolSideEffectMode,
        confirmation_mode: ToolConfirmationMode,
        context: ToolContext,
    ) -> ToolConfirmationRequest | None:
        if confirmation_mode is ToolConfirmationMode.NONE:
            return None
        identity = hashlib.sha256(call.model_dump_json().encode()).hexdigest()
        request_id = uuid5(
            NAMESPACE_URL,
            f"base-agent:tool-confirmation:{context.run_id}:{tool_name}:{identity}",
        )
        return ToolConfirmationRequest(
            id=request_id,
            tool_call_id=call.id,
            tool_name=tool_name,
            side_effect_mode=side_effect_mode,
            prompt=f"Approve execution of Tool '{tool_name}'?",
        )


class ToolSideEffectReceipt(BaseModel):
    """Opaque recorder receipt plus a key available only to Tool code."""

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    token: object
    idempotency_key: str | None = None


class ToolSideEffectRecorderError(RuntimeError):
    """Raised when execution evidence cannot be safely recorded."""


class ToolSideEffectContextError(ToolSideEffectRecorderError):
    """Raised when a governed Tool lacks required execution correlation."""


class ToolSideEffectReplayUnsafeError(ToolSideEffectRecorderError):
    """Raised when replay could duplicate an unprotected external effect."""


class ToolSideEffectRecorder(Protocol):
    """Bracket a Tool transport without receiving its arguments or result."""

    async def start(
        self,
        call: ToolCall,
        *,
        tool_name: str,
        mode: ToolSideEffectMode,
        context: ToolContext,
    ) -> ToolSideEffectReceipt: ...

    async def confirm(self, receipt: ToolSideEffectReceipt) -> None: ...


@runtime_checkable
class GovernedTool(Protocol):
    """Optional Tool extension that explicitly classifies side effects."""

    @property
    def side_effect_mode(self) -> ToolSideEffectMode: ...


@runtime_checkable
class ConfirmableTool(Protocol):
    """Optional Tool extension that declares an approval boundary."""

    @property
    def confirmation_mode(self) -> ToolConfirmationMode: ...
