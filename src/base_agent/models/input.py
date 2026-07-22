"""Provider-neutral human-input suspension models."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
