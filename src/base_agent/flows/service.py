"""Application service for durable Flow lifecycle transitions."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import JsonValue

from base_agent.flows.lease import FlowExecutionLease
from base_agent.flows.lifecycle import AgentInvocation, FlowRunState
from base_agent.flows.models import (
    AgentInvocationRequest,
    AgentInvocationResult,
    FlowDefinition,
)
from base_agent.flows.repository import FlowEventDraft, FlowRepository
from base_agent.models import AgentResultStatus, EventType, RunStatus

_INVOCATION_RESULT_EVENTS = {
    AgentResultStatus.COMPLETED: EventType.AGENT_INVOCATION_COMPLETED,
    AgentResultStatus.FAILED: EventType.AGENT_INVOCATION_FAILED,
    AgentResultStatus.CANCELLED: EventType.AGENT_INVOCATION_CANCELLED,
    AgentResultStatus.INTERRUPTED: EventType.AGENT_INVOCATION_INTERRUPTED,
    AgentResultStatus.LIMIT_REACHED: EventType.AGENT_INVOCATION_LIMIT_REACHED,
    AgentResultStatus.WAITING: EventType.AGENT_INVOCATION_WAITING,
}
_FLOW_TERMINAL_EVENTS = {
    RunStatus.FAILED: EventType.FLOW_FAILED,
    RunStatus.CANCELLED: EventType.FLOW_CANCELLED,
    RunStatus.INTERRUPTED: EventType.FLOW_INTERRUPTED,
    RunStatus.LIMIT_REACHED: EventType.FLOW_LIMIT_REACHED,
}
_INVOCATION_TERMINAL_EVENTS = {
    RunStatus.FAILED: EventType.AGENT_INVOCATION_FAILED,
    RunStatus.CANCELLED: EventType.AGENT_INVOCATION_CANCELLED,
    RunStatus.INTERRUPTED: EventType.AGENT_INVOCATION_INTERRUPTED,
    RunStatus.LIMIT_REACHED: EventType.AGENT_INVOCATION_LIMIT_REACHED,
}


class FlowLifecycle:
    """Persist lifecycle transitions and their events as one atomic operation."""

    def __init__(
        self,
        repository: FlowRepository,
        *,
        execution_lease: FlowExecutionLease | None = None,
    ) -> None:
        self._repository = repository
        self._execution_lease = execution_lease

    async def create(
        self,
        definition: FlowDefinition,
        *,
        run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> FlowRunState:
        state = FlowRunState.create(definition, run_id=run_id, now=now)
        await self._repository.create(
            state,
            events=(
                FlowEventDraft(
                    type=EventType.FLOW_CREATED,
                    data={
                        "definition_id": state.definition_id,
                        "definition_version": state.definition_version,
                        "definition_fingerprint": state.definition_fingerprint,
                        "budget": state.budget.model_dump(mode="json"),
                        "deadline_at": (
                            state.deadline_at.isoformat()
                            if state.deadline_at is not None
                            else None
                        ),
                        "revision": state.revision,
                    },
                ),
            ),
        )
        return state

    async def get(self, run_id: UUID) -> FlowRunState:
        return await self._repository.get(run_id)

    async def record_cancellation_propagation_failure(
        self,
        run_id: UUID,
        invocation: AgentInvocation,
        *,
        error_type: str,
    ) -> None:
        current = await self._repository.get(run_id)
        await self._repository.append_events(
            run_id,
            expected_revision=current.revision,
            events=(
                FlowEventDraft(
                    type=(
                        EventType.AGENT_INVOCATION_CANCELLATION_PROPAGATION_FAILED
                    ),
                    data={
                        **_invocation_data(invocation),
                        "flow_status": current.status.value,
                        "error_type": error_type,
                        "revision": current.revision,
                    },
                ),
            ),
            execution_token=self._execution_token_for(run_id),
        )

    async def start(self, run_id: UUID) -> FlowRunState:
        current = await self._repository.get(run_id)
        state = current.start()
        await self._commit(
            current,
            state,
            FlowEventDraft(
                type=EventType.FLOW_STARTED,
                data={"revision": state.revision},
            ),
        )
        return state

    async def begin_invocation(
        self,
        run_id: UUID,
        request: AgentInvocationRequest,
        *,
        definition: FlowDefinition,
    ) -> FlowRunState:
        current = await self._repository.get(run_id)
        state = current.begin_invocation(request, definition=definition)
        invocation = state.active_invocation
        assert invocation is not None
        await self._commit(
            current,
            state,
            FlowEventDraft(
                type=EventType.AGENT_INVOCATION_STARTED,
                data={
                    **_invocation_data(invocation),
                    "revision": state.revision,
                },
            ),
        )
        return state

    async def settle_invocation(
        self,
        run_id: UUID,
        result: AgentInvocationResult,
    ) -> FlowRunState:
        current = await self._repository.get(run_id)
        state = current.settle_invocation(result)
        settled = state.invocations[-1]
        drafts = [
            FlowEventDraft(
                type=_INVOCATION_RESULT_EVENTS[result.status],
                data={
                    **_invocation_data(settled),
                    "revision": state.revision,
                    "input_tokens": result.usage.input_tokens,
                    "output_tokens": result.usage.output_tokens,
                    "artifact_count": len(result.artifacts),
                },
            )
        ]
        if result.status is AgentResultStatus.WAITING:
            drafts.append(
                FlowEventDraft(
                    type=EventType.FLOW_WAITING,
                    data={
                        "invocation_id": str(settled.id),
                        "revision": state.revision,
                    },
                )
            )
        await self._repository.commit(
            state,
            expected_revision=current.revision,
            events=tuple(drafts),
            execution_token=self._execution_token_for(run_id),
        )
        return state

    async def resume_invocation(
        self,
        run_id: UUID,
        invocation_id: UUID,
    ) -> FlowRunState:
        current = await self._repository.get(run_id)
        state = current.resume_invocation(invocation_id)
        invocation = state.active_invocation
        assert invocation is not None
        await self._repository.commit(
            state,
            expected_revision=current.revision,
            events=(
                FlowEventDraft(
                    type=EventType.AGENT_INVOCATION_RESUMED,
                    data={
                        **_invocation_data(invocation),
                        "revision": state.revision,
                    },
                ),
                FlowEventDraft(
                    type=EventType.FLOW_RESUMED,
                    data={
                        "invocation_id": str(invocation.id),
                        "revision": state.revision,
                    },
                ),
            ),
            execution_token=self._execution_token_for(run_id),
        )
        return state

    async def complete(self, run_id: UUID, output: str) -> FlowRunState:
        current = await self._repository.get(run_id)
        state = current.complete(output)
        await self._commit(
            current,
            state,
            FlowEventDraft(
                type=EventType.FLOW_COMPLETED,
                data={
                    "revision": state.revision,
                    "invocation_count": len(state.invocations),
                    "input_tokens": state.usage.input_tokens,
                    "output_tokens": state.usage.output_tokens,
                    "model_calls": state.model_call_count,
                    "tool_calls": state.tool_call_count,
                },
            ),
        )
        return state

    async def fail(self, run_id: UUID, error: str) -> FlowRunState:
        return await self._terminate(run_id, RunStatus.FAILED, error)

    async def cancel(
        self,
        run_id: UUID,
        error: str = "Flow Run cancellation requested",
    ) -> FlowRunState:
        return await self._terminate(run_id, RunStatus.CANCELLED, error)

    async def interrupt(
        self,
        run_id: UUID,
        error: str = "Flow Run interrupted",
    ) -> FlowRunState:
        return await self._terminate(run_id, RunStatus.INTERRUPTED, error)

    async def reach_limit(
        self,
        run_id: UUID,
        error: str = "Flow Run invocation limit reached",
        *,
        metadata: dict[str, JsonValue] | None = None,
    ) -> FlowRunState:
        return await self._terminate(
            run_id,
            RunStatus.LIMIT_REACHED,
            error,
            event_metadata=metadata,
        )

    async def _terminate(
        self,
        run_id: UUID,
        status: RunStatus,
        error: str,
        *,
        event_metadata: dict[str, JsonValue] | None = None,
    ) -> FlowRunState:
        current = await self._repository.get(run_id)
        active = current.active_invocation
        if status is RunStatus.FAILED:
            state = current.fail(error)
        elif status is RunStatus.CANCELLED:
            state = current.cancel(error)
        elif status is RunStatus.INTERRUPTED:
            state = current.interrupt(error)
        else:
            state = current.reach_limit(error)

        drafts: list[FlowEventDraft] = []
        if active is not None:
            terminated = next(item for item in state.invocations if item.id == active.id)
            drafts.append(
                FlowEventDraft(
                    type=_INVOCATION_TERMINAL_EVENTS[status],
                    data={
                        **_invocation_data(terminated),
                        "revision": state.revision,
                    },
                )
            )
        drafts.append(
            FlowEventDraft(
                type=_FLOW_TERMINAL_EVENTS[status],
                data={
                    "revision": state.revision,
                    "invocation_count": len(state.invocations),
                    **(event_metadata or {}),
                },
            )
        )
        await self._repository.commit(
            state,
            expected_revision=current.revision,
            events=tuple(drafts),
            execution_token=self._execution_token_for(run_id),
        )
        return state

    async def _commit(
        self,
        current: FlowRunState,
        state: FlowRunState,
        event: FlowEventDraft,
    ) -> None:
        await self._repository.commit(
            state,
            expected_revision=current.revision,
            events=(event,),
            execution_token=self._execution_token_for(state.run_id),
        )

    def _execution_token_for(self, run_id: UUID) -> UUID | None:
        if self._execution_lease is None:
            return None
        if self._execution_lease.run_id != run_id:
            raise ValueError(
                f"execution lease belongs to Flow Run "
                f"'{self._execution_lease.run_id}', not '{run_id}'"
            )
        return self._execution_lease.token


def _invocation_data(invocation: AgentInvocation) -> dict[str, JsonValue]:
    return {
        "invocation_id": str(invocation.id),
        "invocation_sequence": invocation.sequence,
        "agent_key": invocation.agent_key,
        "definition_id": invocation.definition_id,
        "definition_version": invocation.definition_version,
        "definition_fingerprint": invocation.definition_fingerprint,
    }
