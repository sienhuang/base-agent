"""Provider-independent ToolResult size enforcement."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models import ToolResult, ToolResultStatus


class ToolResultLimits(BaseModel):
    """Serializable limits applied before a ToolResult enters durable state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_bytes: int = Field(default=262_144, ge=512)


class ToolResultPolicy(Protocol):
    """Replace unsafe result envelopes without mutating their content."""

    def enforce(self, result: ToolResult) -> ToolResult: ...


class BoundedToolResultPolicy:
    """Reject oversized JSON envelopes without truncating structured data."""

    def __init__(
        self,
        limits: ToolResultLimits | None = None,
    ) -> None:
        self.limits = limits or ToolResultLimits()

    def enforce(self, result: ToolResult) -> ToolResult:
        size_bytes = len(result.model_dump_json().encode())
        if size_bytes <= self.limits.max_bytes:
            return result
        return ToolResult(
            tool_name=result.tool_name,
            status=ToolResultStatus.ERROR,
            data={
                "original_size_bytes": size_bytes,
                "limit_bytes": self.limits.max_bytes,
                "original_status": result.status.value,
                "overflow_action": "rejected",
            },
            error_code="tool_result_too_large",
            message="Tool result exceeded the configured size limit",
        )


_policy_contract: type[ToolResultPolicy] = BoundedToolResultPolicy
