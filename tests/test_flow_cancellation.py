import asyncio
from uuid import uuid4

import pytest

from base_agent import (
    AgentDefinition,
    AgentInvocationCancel,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentInvocationResume,
    AgentResultStatus,
    EventType,
    FlowAgent,
    FlowDefinition,
    FlowInput,
    FlowLifecycle,
    InMemoryFlowRepository,
    PendingInput,
    RunStatus,
    SequentialFlowStrategy,
)
from base_agent.testing import ScriptedAgentInvoker, ScriptedAgentOutcome


class BlockingCancellableInvoker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.requests: list[AgentInvocationRequest] = []
        self.cancellations: list[AgentInvocationCancel] = []

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        self.requests.append(request)
        self.started.set()
        await self.cancelled.wait()
        return AgentInvocationResult(
            flow_run_id=request.flow_run_id,
            invocation_id=request.invocation_id,
            agent_key=request.agent_key,
            status=AgentResultStatus.CANCELLED,
            error="child cancellation observed",
        )

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult:
        raise AssertionError("resume is not expected")

    async def cancel(self, request: AgentInvocationCancel) -> None:
        self.cancellations.append(request)
        self.cancelled.set()


class NonCancellableWaitingInvoker:
    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        return AgentInvocationResult(
            flow_run_id=request.flow_run_id,
            invocation_id=request.invocation_id,
            agent_key=request.agent_key,
            status=AgentResultStatus.WAITING,
            pending_input=PendingInput(
                tool_call_id="call-1",
                tool_name="ask_user",
                prompt="Continue?",
            ),
        )

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult:
        raise AssertionError("resume is not expected")


def make_flow() -> FlowDefinition:
    return FlowDefinition(
        id="cancellable-flow",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="worker",
                definition=AgentDefinition(
                    id="worker-agent",
                    version="1.0.0",
                    instructions="Work.",
                ),
            ),
        ),
        strategy="sequential",
    )


@pytest.mark.asyncio
async def test_waiting_flow_cancellation_propagates_once_and_is_idempotent() -> None:
    invoker = ScriptedAgentInvoker(
        {
            "worker": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.WAITING,
                    pending_input=PendingInput(
                        tool_call_id="call-1",
                        tool_name="ask_user",
                        prompt="Continue?",
                    ),
                ),
            ),
        }
    )
    repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=invoker,
    )
    waiting = await strategy.run(make_flow(), FlowInput(prompt="Work."))

    cancelled = await strategy.cancel(waiting.run_id, reason="user stopped")
    repeated = await strategy.cancel(waiting.run_id, reason="user stopped")

    assert cancelled.status is RunStatus.CANCELLED
    assert repeated == cancelled
    assert len(invoker.cancellations) == 1
    assert invoker.cancellations[0].reason == "user stopped"
    state = await repository.get(waiting.run_id)
    assert state.invocations[0].status is RunStatus.CANCELLED
    assert [event.type for event in await repository.events(waiting.run_id)][-2:] == [
        EventType.AGENT_INVOCATION_CANCELLED,
        EventType.FLOW_CANCELLED,
    ]


@pytest.mark.asyncio
async def test_running_flow_cancellation_wins_over_late_child_result() -> None:
    invoker = BlockingCancellableInvoker()
    repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=invoker,
    )
    run_id = uuid4()
    run_task = asyncio.create_task(
        strategy.run(
            make_flow(),
            FlowInput(prompt="Work."),
            run_id=run_id,
        )
    )
    await invoker.started.wait()

    cancelled = await strategy.cancel(run_id)
    late_result = await run_task

    assert cancelled.status is RunStatus.CANCELLED
    assert late_result.status is RunStatus.CANCELLED
    assert len(invoker.cancellations) == 1
    persisted = await repository.get(run_id)
    assert persisted.status is RunStatus.CANCELLED
    assert persisted.invocations[0].status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_missing_cancellation_capability_is_recorded_after_flow_terminal() -> None:
    repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=NonCancellableWaitingInvoker(),
    )
    waiting = await strategy.run(make_flow(), FlowInput(prompt="Work."))

    cancelled = await strategy.cancel(waiting.run_id)

    assert cancelled.status is RunStatus.CANCELLED
    events = await repository.events(waiting.run_id)
    assert events[-1].type is (
        EventType.AGENT_INVOCATION_CANCELLATION_PROPAGATION_FAILED
    )
    assert events[-1].data["error_type"] == "CancellationNotSupported"
    assert events[-1].data["flow_status"] == "cancelled"
