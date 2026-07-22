"""Deterministic composition of independent Supervisor policies."""

from collections.abc import Iterable

from base_agent.models import ToolCall, ToolResult
from base_agent.runtime.context import RuntimeContext
from base_agent.supervision.decision import SupervisionAction, SupervisionDecision
from base_agent.supervision.protocol import Supervisor


class CompositeSupervisor:
    name = "composite-supervisor"

    def __init__(self, policies: Iterable[Supervisor]) -> None:
        self.policies = tuple(policies)

    async def before_model(self, context: RuntimeContext) -> SupervisionDecision:
        for policy in self.policies:
            decision = await policy.before_model(context)
            if decision.action is not SupervisionAction.CONTINUE:
                return decision
        return SupervisionDecision.continue_(self.name)

    async def before_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
    ) -> SupervisionDecision:
        for policy in self.policies:
            decision = await policy.before_tool(context, call)
            if decision.action is not SupervisionAction.CONTINUE:
                return decision
        return SupervisionDecision.continue_(self.name)

    async def after_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
        result: ToolResult,
    ) -> SupervisionDecision:
        for policy in self.policies:
            decision = await policy.after_tool(context, call, result)
            if decision.action is not SupervisionAction.CONTINUE:
                return decision
        return SupervisionDecision.continue_(self.name)
