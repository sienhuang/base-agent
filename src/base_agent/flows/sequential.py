"""Bounded sequential Flow execution over the AgentInvoker boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import UUID

from base_agent.flows.budget import (
    FlowBudgetExceeded,
    FlowBudgetKind,
    FlowBudgetPolicy,
)
from base_agent.flows.lifecycle import (
    AgentInvocation,
    FlowRunState,
    InvalidFlowTransitionError,
)
from base_agent.flows.models import (
    AgentHandoff,
    AgentInvocationCancel,
    AgentInvocationInput,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentInvocationResume,
    FlowDefinition,
    FlowInput,
    FlowResult,
)
from base_agent.flows.protocol import AgentInvoker, CancellableAgentInvoker
from base_agent.flows.service import FlowLifecycle
from base_agent.models import AgentResultStatus, RunStatus, ToolConfirmation
from base_agent.models.run import utc_now


class UnsupportedFlowStrategyError(ValueError):
    """Raised when a strategy receives an incompatible FlowDefinition."""


_TERMINAL_FLOW_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
    RunStatus.LIMIT_REACHED,
}


class SequentialFlowStrategy:
    """Invoke each bound Agent once, in declaration order, with explicit handoffs."""

    name = "sequential"

    def __init__(
        self,
        *,
        lifecycle: FlowLifecycle,
        invoker: AgentInvoker,
        budget_policy: FlowBudgetPolicy | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._lifecycle = lifecycle
        self._invoker = invoker
        self._budget_policy = budget_policy or FlowBudgetPolicy()
        self._clock = clock

    async def run(
        self,
        definition: FlowDefinition,
        flow_input: FlowInput,
        *,
        run_id: UUID | None = None,
    ) -> FlowResult:
        self._require_supported(definition)
        state = await self._lifecycle.create(
            definition,
            run_id=run_id,
            now=self._clock(),
        )
        state = await self._lifecycle.start(state.run_id)
        return await self._advance(
            definition,
            state,
            flow_input,
            previous=None,
        )

    async def resume(
        self,
        definition: FlowDefinition,
        run_id: UUID,
        user_input: str,
    ) -> FlowResult:
        return await self._resume_waiting(
            definition,
            run_id,
            user_input=user_input,
            confirmation=None,
        )

    async def confirm(
        self,
        definition: FlowDefinition,
        run_id: UUID,
        confirmation: ToolConfirmation,
    ) -> FlowResult:
        return await self._resume_waiting(
            definition,
            run_id,
            user_input=None,
            confirmation=confirmation,
        )

    async def _resume_waiting(
        self,
        definition: FlowDefinition,
        run_id: UUID,
        *,
        user_input: str | None,
        confirmation: ToolConfirmation | None,
    ) -> FlowResult:
        self._require_supported(definition)
        waiting = await self._lifecycle.get(run_id)
        self._require_definition(waiting, definition)
        violation = self._budget_policy.before_transport(
            waiting,
            now=self._clock(),
            new_invocation=False,
        )
        if violation is not None:
            return await self._reach_budget_limit(waiting.run_id, violation)
        active = waiting.active_invocation
        if active is None:
            raise ValueError("Flow Run has no waiting AgentInvocation")
        flow_input = FlowInput(
            prompt=active.input.prompt,
            attachments=active.input.attachments,
        )
        await self._lifecycle.resume_invocation(run_id, active.id)
        request = AgentInvocationResume(
            flow_run_id=run_id,
            invocation_id=active.id,
            agent_key=active.agent_key,
            user_input=user_input,
            confirmation=confirmation,
        )
        try:
            result = await self._within_deadline(
                waiting,
                self._invoker.resume(request),
            )
        except TimeoutError:
            return await self._timeout_limit(waiting)
        except asyncio.CancelledError:
            await self._lifecycle.interrupt(
                run_id,
                "Sequential Flow resume cancelled",
            )
            raise
        except Exception as exc:
            return await self._transport_failure(
                run_id,
                agent_key=active.agent_key,
                operation="resume",
                error=exc,
            )
        try:
            state = await self._lifecycle.settle_invocation(run_id, result)
        except InvalidFlowTransitionError:
            terminal = await self._terminal_after_race(run_id)
            if terminal is not None:
                return terminal
            raise
        except ValueError as exc:
            return await self._transport_failure(
                run_id,
                agent_key=active.agent_key,
                operation="resume returned an invalid result",
                error=exc,
            )
        violation = self._budget_policy.after_transport(
            state,
            now=self._clock(),
        )
        if violation is not None:
            return await self._reach_budget_limit(state.run_id, violation)
        terminal = await self._handle_outcome(state, result)
        if terminal is not None:
            return terminal
        return await self._advance(
            definition,
            state,
            flow_input,
            previous=result,
        )

    async def cancel(
        self,
        run_id: UUID,
        *,
        reason: str = "Flow cancellation requested",
    ) -> FlowResult:
        if not reason.strip():
            raise ValueError("Flow cancellation reason must not be blank")
        if len(reason) > 1_000:
            raise ValueError("Flow cancellation reason must not exceed 1000 characters")
        current = await self._lifecycle.get(run_id)
        if current.status is RunStatus.CANCELLED:
            return _flow_result(current)
        if current.status in _TERMINAL_FLOW_STATUSES:
            raise InvalidFlowTransitionError(
                f"cannot cancel Flow Run in state '{current.status.value}'"
            )
        active = current.active_invocation
        cancelled = await self._lifecycle.cancel(run_id, reason)
        if active is not None:
            await self._propagate_cancellation(active, reason=reason)
        return _flow_result(cancelled)

    async def _advance(
        self,
        definition: FlowDefinition,
        state: FlowRunState,
        flow_input: FlowInput,
        *,
        previous: AgentInvocationResult | None,
    ) -> FlowResult:
        while len(state.invocations) < len(definition.agents):
            violation = self._budget_policy.before_transport(
                state,
                now=self._clock(),
                new_invocation=True,
            )
            if violation is not None:
                return await self._reach_budget_limit(state.run_id, violation)
            binding = definition.agents[len(state.invocations)]
            invocation_input = AgentInvocationInput(
                prompt=flow_input.prompt,
                handoff=_handoff(previous),
                attachments=flow_input.attachments,
            )
            request = AgentInvocationRequest(
                flow_run_id=state.run_id,
                sequence=len(state.invocations) + 1,
                agent_key=binding.key,
                definition_id=binding.definition.id,
                definition_version=binding.definition.version,
                definition_fingerprint=binding.definition.fingerprint,
                input=invocation_input,
            )
            state = await self._lifecycle.begin_invocation(
                state.run_id,
                request,
                definition=definition,
            )
            try:
                result = await self._within_deadline(
                    state,
                    self._invoker.invoke(request),
                )
            except TimeoutError:
                return await self._timeout_limit(state)
            except asyncio.CancelledError:
                await self._lifecycle.interrupt(
                    state.run_id,
                    "Sequential Flow execution cancelled",
                )
                raise
            except Exception as exc:
                return await self._transport_failure(
                    state.run_id,
                    agent_key=binding.key,
                    operation="invoke",
                    error=exc,
                )
            try:
                state = await self._lifecycle.settle_invocation(
                    state.run_id,
                    result,
                )
            except InvalidFlowTransitionError:
                terminal = await self._terminal_after_race(state.run_id)
                if terminal is not None:
                    return terminal
                raise
            except ValueError as exc:
                return await self._transport_failure(
                    state.run_id,
                    agent_key=binding.key,
                    operation="invoke returned an invalid result",
                    error=exc,
                )
            violation = self._budget_policy.after_transport(
                state,
                now=self._clock(),
            )
            if violation is not None:
                return await self._reach_budget_limit(state.run_id, violation)
            terminal = await self._handle_outcome(state, result)
            if terminal is not None:
                return terminal
            previous = result

        assert previous is not None
        completed = await self._lifecycle.complete(
            state.run_id,
            previous.output or "",
        )
        return _flow_result(completed)

    async def _handle_outcome(
        self,
        state: FlowRunState,
        result: AgentInvocationResult,
    ) -> FlowResult | None:
        if result.status is AgentResultStatus.COMPLETED:
            return None
        if result.status is AgentResultStatus.WAITING:
            return _flow_result(state)
        error = result.error or f"Agent '{result.agent_key}' did not complete"
        if result.status is AgentResultStatus.FAILED:
            terminal = await self._lifecycle.fail(state.run_id, error)
        elif result.status is AgentResultStatus.CANCELLED:
            terminal = await self._lifecycle.cancel(state.run_id, error)
        elif result.status is AgentResultStatus.INTERRUPTED:
            terminal = await self._lifecycle.interrupt(state.run_id, error)
        else:
            terminal = await self._lifecycle.reach_limit(state.run_id, error)
        return _flow_result(terminal)

    async def _transport_failure(
        self,
        run_id: UUID,
        *,
        agent_key: str,
        operation: str,
        error: Exception,
    ) -> FlowResult:
        terminal = await self._terminal_after_race(run_id)
        if terminal is not None:
            return terminal
        message = (
            f"Agent '{agent_key}' {operation} raised "
            f"{type(error).__name__}: {error}"
        )
        failed = await self._lifecycle.fail(run_id, message)
        return _flow_result(failed)

    async def _within_deadline(
        self,
        state: FlowRunState,
        operation: Awaitable[AgentInvocationResult],
    ) -> AgentInvocationResult:
        remaining = self._budget_policy.remaining_seconds(
            state,
            now=self._clock(),
        )
        if remaining is None:
            return await operation
        async with asyncio.timeout(remaining):
            return await operation

    async def _timeout_limit(self, state: FlowRunState) -> FlowResult:
        consumption = self._budget_policy.consumption(
            state,
            now=self._clock(),
        )
        assert state.budget.timeout_seconds is not None
        violation = FlowBudgetExceeded(
            kind=FlowBudgetKind.TIMEOUT_SECONDS,
            limit=state.budget.timeout_seconds,
            actual=max(
                state.budget.timeout_seconds,
                consumption.elapsed_seconds,
            ),
        )
        return await self._reach_budget_limit(state.run_id, violation)

    async def _reach_budget_limit(
        self,
        run_id: UUID,
        violation: FlowBudgetExceeded,
    ) -> FlowResult:
        current = await self._lifecycle.get(run_id)
        if current.status in _TERMINAL_FLOW_STATUSES:
            return _flow_result(current)
        active = current.active_invocation
        limited = await self._lifecycle.reach_limit(
            run_id,
            violation.message,
            metadata={
                "budget": violation.model_dump(mode="json"),
            },
        )
        if active is not None and active.status is RunStatus.WAITING:
            await self._propagate_cancellation(
                active,
                reason=violation.message,
            )
        return _flow_result(limited)

    async def _propagate_cancellation(
        self,
        invocation: AgentInvocation,
        *,
        reason: str,
    ) -> None:
        request = AgentInvocationCancel(
            flow_run_id=invocation.flow_run_id,
            invocation_id=invocation.id,
            agent_key=invocation.agent_key,
            reason=reason,
        )
        if not isinstance(self._invoker, CancellableAgentInvoker):
            await self._lifecycle.record_cancellation_propagation_failure(
                invocation.flow_run_id,
                invocation,
                error_type="CancellationNotSupported",
            )
            return
        try:
            await self._invoker.cancel(request)
        except Exception as exc:
            await self._lifecycle.record_cancellation_propagation_failure(
                invocation.flow_run_id,
                invocation,
                error_type=type(exc).__name__,
            )

    async def _terminal_after_race(self, run_id: UUID) -> FlowResult | None:
        current = await self._lifecycle.get(run_id)
        if current.status in _TERMINAL_FLOW_STATUSES:
            return _flow_result(current)
        return None

    def _require_supported(self, definition: FlowDefinition) -> None:
        if definition.strategy != self.name:
            raise UnsupportedFlowStrategyError(
                f"SequentialFlowStrategy cannot execute strategy "
                f"'{definition.strategy}'"
            )

    @staticmethod
    def _require_definition(
        state: FlowRunState,
        definition: FlowDefinition,
    ) -> None:
        if (
            state.definition_id != definition.id
            or state.definition_version != definition.version
            or state.definition_fingerprint != definition.fingerprint
        ):
            raise ValueError("FlowDefinition does not match this Flow Run")


def _handoff(result: AgentInvocationResult | None) -> AgentHandoff | None:
    if result is None:
        return None
    return AgentHandoff(
        source_agent_key=result.agent_key,
        summary=result.output or "Agent completed without textual output.",
        data=result.metadata,
        artifacts=result.artifacts,
    )


def _flow_result(state: FlowRunState) -> FlowResult:
    active = state.active_invocation
    pending_input = None
    waiting_invocation_id = None
    if state.status is RunStatus.WAITING:
        assert active is not None
        assert active.result is not None
        pending_input = active.result.pending_input
        waiting_invocation_id = active.id
    artifacts = tuple(
        artifact
        for invocation in state.invocations
        if invocation.result is not None
        for artifact in invocation.result.artifacts
    )
    return FlowResult(
        run_id=state.run_id,
        status=state.status,
        output=state.output,
        usage=state.usage,
        artifacts=artifacts,
        invocation_count=len(state.invocations),
        model_call_count=state.model_call_count,
        tool_call_count=state.tool_call_count,
        waiting_invocation_id=waiting_invocation_id,
        pending_input=pending_input,
        error=state.error,
    )
