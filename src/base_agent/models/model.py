"""Contracts exchanged with a model provider."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.models.artifact import Attachment
from base_agent.models.memory import MemoryMatch
from base_agent.models.message import Message, ToolCall


class ToolChoice(StrEnum):
    """Portable tool selection modes understood by the core runtime."""

    AUTO = "auto"
    NONE = "none"
    REQUIRED = "required"


class ToolDefinition(BaseModel):
    """The model-facing description and input schema of a tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = Field(min_length=1)
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})

    @model_validator(mode="after")
    def validate_input_schema(self) -> Self:
        if self.input_schema.get("type") != "object":
            raise ValueError("tool input_schema must describe a JSON object")
        return self


class TokenUsage(BaseModel):
    """Provider-reported token usage without provider-specific accounting fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="before")
    @classmethod
    def populate_and_validate_total(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data

        normalized = dict(data)
        expected = normalized.get("input_tokens", 0) + normalized.get("output_tokens", 0)
        total = normalized.get("total_tokens", 0)
        if total not in (0, expected):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        normalized["total_tokens"] = expected
        return normalized

    def __add__(self, other: TokenUsage) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
        )


class ModelRequest(BaseModel):
    """A complete, provider-neutral model invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[Message, ...] = Field(min_length=1)
    tools: tuple[ToolDefinition, ...] = ()
    tool_choice: ToolChoice = ToolChoice.AUTO
    model: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()
    memories: tuple[MemoryMatch, ...] = ()

    @model_validator(mode="after")
    def validate_tool_choice(self) -> Self:
        if self.tool_choice is ToolChoice.REQUIRED and not self.tools:
            raise ValueError("tool_choice='required' requires at least one tool")
        return self


class ModelResponse(BaseModel):
    """A normalized response returned by any model provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    provider_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        if self.content is None and not self.tool_calls:
            raise ValueError("model responses require content or tool_calls")
        return self

    def to_assistant_message(self) -> Message:
        return Message.assistant(self.content, tool_calls=self.tool_calls)
