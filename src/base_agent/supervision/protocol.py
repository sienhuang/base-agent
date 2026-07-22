"""Supervisor hooks called at safe Runtime boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from base_agent.models import ToolCall, ToolResult
from base_agent.supervision.decision import SupervisionDecision

if TYPE_CHECKING:
    from base_agent.runtime.context import RuntimeContext


@runtime_checkable
class Supervisor(Protocol):
    @property
    def name(self) -> str: ...

    async def before_model(self, context: RuntimeContext) -> SupervisionDecision: ...

    async def before_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
    ) -> SupervisionDecision: ...

    async def after_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
        result: ToolResult,
    ) -> SupervisionDecision: ...


class BaseSupervisor:
    """Convenience base with no-op hooks for custom policies."""

    name = "base-supervisor"

    async def before_model(self, context: RuntimeContext) -> SupervisionDecision:
        return SupervisionDecision.continue_(self.name)

    async def before_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
    ) -> SupervisionDecision:
        return SupervisionDecision.continue_(self.name)

    async def after_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
        result: ToolResult,
    ) -> SupervisionDecision:
        return SupervisionDecision.continue_(self.name)
