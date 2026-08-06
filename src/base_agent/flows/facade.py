"""Small public facade over the durable Flow execution components."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from uuid import UUID

from base_agent.agent import Agent
from base_agent.flows.models import (
    FlowAgent,
    FlowBudget,
    FlowDefinition,
    FlowInput,
    FlowResult,
)
from base_agent.flows.repository import FlowRepository, InMemoryFlowRepository
from base_agent.flows.runtime_invoker import AgentRuntimeInvoker
from base_agent.flows.sequential import SequentialFlowStrategy
from base_agent.flows.service import FlowLifecycle
from base_agent.models import (
    Attachment,
    PendingInput,
    RunStatus,
    RuntimeEvent,
    ToolConfirmation,
)


class Flow:
    """Executable Agent composition that hides lifecycle and invoker wiring."""

    def __init__(
        self,
        definition: FlowDefinition,
        strategy: SequentialFlowStrategy,
        repository: FlowRepository,
    ) -> None:
        self.definition = definition
        self._strategy = strategy
        self._repository = repository

    @classmethod
    def sequence(
        cls,
        agents: Mapping[str, Agent] | Iterable[tuple[str, Agent]],
        *,
        id: str,
        version: str = "1.0.0",
        budget: FlowBudget | None = None,
    ) -> Flow:
        """Build an ordered Flow directly from definition-backed Agents."""
        bindings = tuple(
            agents.items() if isinstance(agents, Mapping) else agents
        )
        if not bindings:
            raise ValueError("Flow.sequence requires at least one Agent")
        configured: dict[str, Agent] = {}
        flow_agents: list[FlowAgent] = []
        for key, agent in bindings:
            if key in configured:
                raise ValueError(f"Flow Agent key '{key}' is duplicated")
            if agent.definition is None:
                raise ValueError(
                    f"Flow Agent '{key}' must use AgentDefinition"
                )
            configured[key] = agent
            flow_agents.append(
                FlowAgent(key=key, definition=agent.definition)
            )
        definition = FlowDefinition(
            id=id,
            version=version,
            agents=tuple(flow_agents),
            strategy=SequentialFlowStrategy.name,
            budget=budget or FlowBudget(),
        )
        active_repository = InMemoryFlowRepository()
        strategy = SequentialFlowStrategy(
            lifecycle=FlowLifecycle(active_repository),
            invoker=AgentRuntimeInvoker(definition, configured),
        )
        return cls(definition, strategy, active_repository)

    async def run(
        self,
        prompt: str,
        *,
        attachments: Iterable[Attachment] = (),
        run_id: UUID | None = None,
    ) -> FlowRun:
        """Run the composition and return one resumable handle."""
        result = await self._strategy.run(
            self.definition,
            FlowInput(
                prompt=prompt,
                attachments=tuple(attachments),
            ),
            run_id=run_id,
        )
        return FlowRun(self, result)


class FlowRun:
    """Latest result plus simple controls for one Flow execution."""

    def __init__(self, flow: Flow, result: FlowResult) -> None:
        self._flow = flow
        self.result = result

    @property
    def id(self) -> UUID:
        return self.result.run_id

    @property
    def status(self) -> RunStatus:
        return self.result.status

    @property
    def output(self) -> str | None:
        return self.result.output

    @property
    def waiting(self) -> bool:
        return self.result.status is RunStatus.WAITING

    @property
    def pending_input(self) -> PendingInput | None:
        return self.result.pending_input

    async def resume(self, user_input: str) -> FlowRun:
        result = await self._flow._strategy.resume(
            self._flow.definition,
            self.id,
            user_input,
        )
        return FlowRun(self._flow, result)

    async def confirm(
        self,
        confirmation: ToolConfirmation,
    ) -> FlowRun:
        result = await self._flow._strategy.confirm(
            self._flow.definition,
            self.id,
            confirmation,
        )
        return FlowRun(self._flow, result)

    async def cancel(
        self,
        *,
        reason: str = "Flow cancellation requested",
    ) -> FlowRun:
        result = await self._flow._strategy.cancel(
            self.id,
            reason=reason,
        )
        return FlowRun(self._flow, result)

    async def events(
        self,
        *,
        after_sequence: int = 0,
    ) -> tuple[RuntimeEvent, ...]:
        events = await self._flow._repository.events(self.id)
        return tuple(
            event for event in events if event.sequence > after_sequence
        )
