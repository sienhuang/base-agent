"""Declarative configuration for an agent."""

from pydantic import BaseModel, ConfigDict, Field


class AgentProfile(BaseModel):
    """Instructions, model route, capabilities, and limits for one agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    instructions: str = Field(min_length=1)
    model: str | None = None
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    max_steps: int = Field(default=10, ge=1)
    max_tool_calls: int = Field(default=50, ge=1)
    duplicate_tool_call_threshold: int = Field(default=3, ge=2)
    max_consecutive_tool_failures: int = Field(default=3, ge=1)
