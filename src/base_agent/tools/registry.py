"""Deterministic registry of tools enabled for an Agent."""

from collections.abc import Iterable

from base_agent.models import ToolDefinition
from base_agent.tools.protocol import Tool


class DuplicateToolError(ValueError):
    """Raised when two tools claim the same public name."""


class ToolNotFoundError(LookupError):
    """Raised when an AgentProfile enables a tool that was not registered."""


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for registered_tool in tools:
            self.add(registered_tool)

    def add(self, registered_tool: Tool) -> None:
        name = registered_tool.definition.name
        if name in self._tools:
            raise DuplicateToolError(f"tool '{name}' is already registered")
        self._tools[name] = registered_tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def require(self, names: Iterable[str]) -> tuple[Tool, ...]:
        resolved: list[Tool] = []
        for name in names:
            registered_tool = self.get(name)
            if registered_tool is None:
                raise ToolNotFoundError(f"tool '{name}' is not registered")
            resolved.append(registered_tool)
        return tuple(resolved)

    def definitions(self, names: Iterable[str]) -> tuple[ToolDefinition, ...]:
        return tuple(item.definition for item in self.require(names))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def __len__(self) -> int:
        return len(self._tools)
