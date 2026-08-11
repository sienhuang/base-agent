"""Immutable application-owned Agent definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from base_agent.models import ToolChoice

_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    id: str
    version: str
    instructions: str
    description: str = ""
    model: str | None = None
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    tool_choice: ToolChoice = ToolChoice.AUTO
    response_model: type[BaseModel] | None = None
    max_model_turns: int = 10
    max_tool_calls_per_turn: int = 3

    def __post_init__(self) -> None:
        if not self.id or not _ID_PATTERN.fullmatch(self.id):
            raise ValueError("agent id must contain only letters, numbers, '.', '_' or '-'")
        if not self.version or not _VERSION_PATTERN.fullmatch(self.version):
            raise ValueError("agent version is invalid")
        if not self.instructions.strip():
            raise ValueError("agent instructions must not be blank")
        if self.max_model_turns < 1:
            raise ValueError("max_model_turns must be at least 1")
        if self.max_tool_calls_per_turn < 1:
            raise ValueError("max_tool_calls_per_turn must be at least 1")
        if len(set(self.tools)) != len(self.tools):
            raise ValueError("agent tools must not contain duplicates")
        if len(set(self.skills)) != len(self.skills):
            raise ValueError("agent skills must not contain duplicates")
