"""Deterministic whole-Flow harness built on the production lifecycle path."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from base_agent.flows import (
    AgentInvocation,
    AgentInvocationCancel,
    AgentInvocationRequest,
    AgentInvocationResume,
    FlowDefinition,
    FlowInput,
    FlowLifecycle,
    FlowResult,
    FlowRunState,
    InMemoryFlowRepository,
    SequentialFlowStrategy,
)
from base_agent.models import EventType, RuntimeEvent, ToolConfirmation
from base_agent.models.run import utc_now
from base_agent.testing.invoker import (
    ScriptedAgentInvoker,
    ScriptedAgentOutcome,
)


class FlowTestRun(BaseModel):
    """Immutable cumulative evidence captured from one Flow Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: FlowResult
    state: FlowRunState
    events: tuple[RuntimeEvent, ...]
    requests: tuple[AgentInvocationRequest, ...]
    resumes: tuple[AgentInvocationResume, ...]
    cancellations: tuple[AgentInvocationCancel, ...]

    @property
    def event_types(self) -> tuple[EventType, ...]:
        return tuple(event.type for event in self.events)

    @property
    def agent_keys(self) -> tuple[str, ...]:
        return tuple(request.agent_key for request in self.requests)

    @property
    def invocations(self) -> tuple[AgentInvocation, ...]:
        return self.state.invocations

    def requests_for(self, agent_key: str) -> tuple[AgentInvocationRequest, ...]:
        """Return invocation requests for one stable Agent key."""
        return tuple(
            request for request in self.requests if request.agent_key == agent_key
        )


class FlowTestHarness:
    """Run a sequential Flow with scripted Agents and capture persisted evidence."""

    def __init__(
        self,
        definition: FlowDefinition,
        outcomes: Mapping[str, Iterable[ScriptedAgentOutcome]],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        expected_keys = {binding.key for binding in definition.agents}
        extra_keys = set(outcomes) - expected_keys
        if extra_keys:
            raise ValueError(
                f"scripted outcomes contain unknown Agent keys: "
                f"{sorted(extra_keys)}"
            )
        self.definition = definition
        self.repository = InMemoryFlowRepository()
        self.invoker = ScriptedAgentInvoker(outcomes)
        self.strategy = SequentialFlowStrategy(
            lifecycle=FlowLifecycle(self.repository),
            invoker=self.invoker,
            clock=clock,
        )
        self._tracked_run_ids: set[UUID] = set()

    async def run(
        self,
        flow_input: FlowInput | str,
        *,
        run_id: UUID | None = None,
    ) -> FlowTestRun:
        """Execute a new Flow Run and return its complete evidence snapshot."""
        active_run_id = run_id or uuid4()
        if active_run_id in self._tracked_run_ids:
            raise ValueError(
                f"Flow Run '{active_run_id}' is already tracked by this harness"
            )
        self._tracked_run_ids.add(active_run_id)
        resolved_input = (
            FlowInput(prompt=flow_input)
            if isinstance(flow_input, str)
            else flow_input
        )
        result = await self.strategy.run(
            self.definition,
            resolved_input,
            run_id=active_run_id,
        )
        return await self._capture(result)

    async def resume(self, run_id: UUID, user_input: str) -> FlowTestRun:
        """Resume a tracked waiting Flow and return cumulative evidence."""
        self._require_tracked(run_id)
        result = await self.strategy.resume(
            self.definition,
            run_id,
            user_input,
        )
        return await self._capture(result)

    async def confirm(
        self,
        run_id: UUID,
        confirmation: ToolConfirmation,
    ) -> FlowTestRun:
        """Confirm a tracked waiting Flow Tool request."""
        self._require_tracked(run_id)
        result = await self.strategy.confirm(
            self.definition,
            run_id,
            confirmation,
        )
        return await self._capture(result)

    async def cancel(
        self,
        run_id: UUID,
        *,
        reason: str = "Flow cancellation requested",
    ) -> FlowTestRun:
        """Cancel a tracked Flow and capture parent and child cancellation evidence."""
        self._require_tracked(run_id)
        result = await self.strategy.cancel(run_id, reason=reason)
        return await self._capture(result)

    async def _capture(self, result: FlowResult) -> FlowTestRun:
        run_id = result.run_id
        return FlowTestRun(
            result=result,
            state=await self.repository.get(run_id),
            events=await self.repository.events(run_id),
            requests=tuple(
                request
                for request in self.invoker.requests
                if request.flow_run_id == run_id
            ),
            resumes=tuple(
                request
                for request in self.invoker.resumes
                if request.flow_run_id == run_id
            ),
            cancellations=tuple(
                request
                for request in self.invoker.cancellations
                if request.flow_run_id == run_id
            ),
        )

    def _require_tracked(self, run_id: UUID) -> None:
        if run_id not in self._tracked_run_ids:
            raise ValueError(
                f"Flow Run '{run_id}' is not tracked by this harness"
            )
