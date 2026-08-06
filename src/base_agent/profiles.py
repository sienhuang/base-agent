"""Declarative, provider-independent definitions for an Agent."""

import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field


class AgentProfile(BaseModel):
    """Legacy runtime profile retained for backwards-compatible composition."""

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
    max_tool_result_bytes: int = Field(default=262_144, ge=512)


class AgentDefinition(AgentProfile):
    """Versioned description of who an Agent is and which capabilities it may use."""

    version: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._+-]*$",
    )

    def to_profile(self) -> AgentProfile:
        """Project the definition onto the current Runtime's profile contract."""
        return AgentProfile.model_validate(
            self.model_dump(exclude={"version"}, mode="python")
        )

    @property
    def fingerprint(self) -> str:
        """Return a stable content hash for audit and compatibility checks."""
        payload = {
            "duplicate_tool_call_threshold": self.duplicate_tool_call_threshold,
            "id": self.id,
            "instructions": self.instructions,
            "max_consecutive_tool_failures": self.max_consecutive_tool_failures,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_tool_result_bytes": self.max_tool_result_bytes,
            "model": self.model,
            "permissions": sorted(self.permissions),
            "skills": list(self.skills),
            "tools": list(self.tools),
            "version": self.version,
        }
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return hashlib.sha256(canonical).hexdigest()
