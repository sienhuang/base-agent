"""Protocol implemented by local and remote tools."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from base_agent.models import ToolDefinition


@runtime_checkable
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    @property
    def permissions(self) -> frozenset[str]: ...

    @property
    def timeout_seconds(self) -> float: ...

    async def invoke(self, arguments: Mapping[str, Any]) -> Any: ...
