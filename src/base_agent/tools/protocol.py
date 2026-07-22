"""Protocol implemented by local and remote tools."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from base_agent.models import ToolDefinition
from base_agent.tools.context import ToolContext


@runtime_checkable
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def permissions(self) -> frozenset[str]: ...

    @property
    def timeout_seconds(self) -> float: ...

    async def invoke(self, arguments: Mapping[str, Any]) -> Any: ...


@runtime_checkable
class ContextualTool(Protocol):
    """Optional extension for Tools that consume execution-scoped capabilities."""

    async def invoke_with_context(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> Any: ...
