"""Conversation and tool-call models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MessageRole(StrEnum):
    """Roles supported by the provider-neutral message contract."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCall(BaseModel):
    """A provider-neutral request to execute one named tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """One validated message in an agent conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def validate_role_fields(self) -> Self:
        """Reject provider payloads that are ambiguous at the runtime boundary."""
        if self.role is MessageRole.ASSISTANT:
            if self.content is None and not self.tool_calls:
                raise ValueError("assistant messages require content or tool_calls")
            if self.tool_call_id is not None:
                raise ValueError("assistant messages cannot set tool_call_id")
            return self

        if self.tool_calls:
            raise ValueError("only assistant messages may contain tool_calls")

        if self.content is None:
            raise ValueError(f"{self.role.value} messages require content")

        if self.role is MessageRole.TOOL:
            if not self.tool_call_id:
                raise ValueError("tool messages require tool_call_id")
        elif self.tool_call_id is not None:
            raise ValueError("only tool messages may set tool_call_id")

        return self

    @classmethod
    def system(cls, content: str) -> Message:
        return cls(role=MessageRole.SYSTEM, content=content)

    @classmethod
    def user(cls, content: str) -> Message:
        return cls(role=MessageRole.USER, content=content)

    @classmethod
    def assistant(
        cls,
        content: str | None = None,
        *,
        tool_calls: tuple[ToolCall, ...] = (),
    ) -> Message:
        return cls(role=MessageRole.ASSISTANT, content=content, tool_calls=tool_calls)

    @classmethod
    def tool(cls, content: str, *, tool_call_id: str) -> Message:
        return cls(role=MessageRole.TOOL, content=content, tool_call_id=tool_call_id)
