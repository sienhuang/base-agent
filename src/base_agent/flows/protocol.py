"""Execution boundary used by Flow strategies to invoke named Agents."""

from typing import Protocol, runtime_checkable
from uuid import UUID

from base_agent.flows.models import (
    AgentInvocationCancel,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentInvocationResume,
    FlowDefinition,
    FlowInput,
    FlowResult,
)
from base_agent.models import ToolConfirmation


@runtime_checkable
class AgentInvoker(Protocol):
    """Invoke or resume an Agent without exposing Flow strategies to Agent Runtime."""

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult: ...

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult: ...


@runtime_checkable
class CancellableAgentInvoker(Protocol):
    """Optional capability for propagating Flow termination to an Agent Runtime."""

    async def cancel(self, request: AgentInvocationCancel) -> None: ...


@runtime_checkable
class FlowStrategy(Protocol):
    """Run and resume one supported Flow definition without exposing its internals."""

    async def run(
        self,
        definition: FlowDefinition,
        flow_input: FlowInput,
        *,
        run_id: UUID | None = None,
    ) -> FlowResult: ...

    async def resume(
        self,
        definition: FlowDefinition,
        run_id: UUID,
        user_input: str,
    ) -> FlowResult: ...

    async def confirm(
        self,
        definition: FlowDefinition,
        run_id: UUID,
        confirmation: ToolConfirmation,
    ) -> FlowResult: ...

    async def cancel(
        self,
        run_id: UUID,
        *,
        reason: str = "Flow cancellation requested",
    ) -> FlowResult: ...
