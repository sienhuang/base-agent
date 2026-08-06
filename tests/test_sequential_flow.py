import asyncio
from uuid import uuid4

import pytest

from base_agent import (
    AgentDefinition,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentInvocationResume,
    AgentResultStatus,
    EventType,
    FlowAgent,
    FlowDefinition,
    FlowInput,
    FlowLifecycle,
    FlowStrategy,
    InMemoryFlowRepository,
    PendingInput,
    RunStatus,
    SequentialFlowStrategy,
    TokenUsage,
    UnsupportedFlowStrategyError,
)
from base_agent.testing import (
    ScriptedAgentInvoker,
    ScriptedAgentOutcome,
)


class BlockingInvoker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        self.started.set()
        await self.release.wait()
        raise AssertionError("blocking invocation should be cancelled")

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult:
        raise AssertionError("resume is not expected")


class MismatchedResultInvoker:
    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        return AgentInvocationResult(
            flow_run_id=request.flow_run_id,
            invocation_id=uuid4(),
            agent_key=request.agent_key,
            status=AgentResultStatus.COMPLETED,
            output="invalid",
        )

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult:
        raise AssertionError("resume is not expected")


def make_flow(*, max_invocations: int = 3, strategy: str = "sequential") -> FlowDefinition:
    return FlowDefinition(
        id="research-report",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="researcher",
                definition=AgentDefinition(
                    id="research-agent",
                    version="1.0.0",
                    instructions="Research.",
                ),
            ),
            FlowAgent(
                key="writer",
                definition=AgentDefinition(
                    id="writer-agent",
                    version="1.0.0",
                    instructions="Write.",
                ),
            ),
        ),
        strategy=strategy,
        max_invocations=max_invocations,
    )


def make_strategy(
    outcomes: dict[str, tuple[ScriptedAgentOutcome, ...]],
) -> tuple[SequentialFlowStrategy, ScriptedAgentInvoker, InMemoryFlowRepository]:
    repository = InMemoryFlowRepository()
    invoker = ScriptedAgentInvoker(outcomes)
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=invoker,
    )
    return strategy, invoker, repository


@pytest.mark.asyncio
async def test_sequential_flow_completes_with_explicit_handoff() -> None:
    strategy, invoker, repository = make_strategy(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="Three sources support the conclusion.",
                    usage=TokenUsage(input_tokens=5, output_tokens=2),
                    metadata={"confidence": 0.9},
                ),
            ),
            "writer": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="Final report.",
                    usage=TokenUsage(input_tokens=4, output_tokens=3),
                ),
            ),
        }
    )
    assert isinstance(strategy, FlowStrategy)

    result = await strategy.run(
        make_flow(),
        FlowInput(prompt="Research and write about harness engineering."),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Final report."
    assert result.invocation_count == 2
    assert result.usage == TokenUsage(input_tokens=9, output_tokens=5)
    assert [request.agent_key for request in invoker.requests] == [
        "researcher",
        "writer",
    ]
    handoff = invoker.requests[1].input.handoff
    assert handoff is not None
    assert handoff.source_agent_key == "researcher"
    assert handoff.summary == "Three sources support the conclusion."
    assert handoff.data == {"confidence": 0.9}
    assert [event.type for event in await repository.events(result.run_id)] == [
        EventType.FLOW_CREATED,
        EventType.FLOW_STARTED,
        EventType.AGENT_INVOCATION_STARTED,
        EventType.AGENT_INVOCATION_COMPLETED,
        EventType.AGENT_INVOCATION_STARTED,
        EventType.AGENT_INVOCATION_COMPLETED,
        EventType.FLOW_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_sequential_flow_waits_and_resumes_before_next_agent() -> None:
    pending = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Which region?",
    )
    strategy, invoker, repository = make_strategy(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.WAITING,
                    pending_input=pending,
                    usage=TokenUsage(input_tokens=3, output_tokens=1),
                ),
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="APAC research.",
                    usage=TokenUsage(input_tokens=6, output_tokens=2),
                ),
            ),
            "writer": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="APAC report.",
                    usage=TokenUsage(input_tokens=2, output_tokens=2),
                ),
            ),
        }
    )
    definition = make_flow()

    waiting = await strategy.run(
        definition,
        FlowInput(prompt="Research the market."),
    )

    assert waiting.status is RunStatus.WAITING
    assert waiting.pending_input == pending
    assert waiting.waiting_invocation_id is not None
    assert len(invoker.requests) == 1

    completed = await strategy.resume(
        definition,
        waiting.run_id,
        "APAC",
    )

    assert completed.status is RunStatus.COMPLETED
    assert completed.output == "APAC report."
    assert completed.usage == TokenUsage(input_tokens=8, output_tokens=4)
    assert len(invoker.resumes) == 1
    assert [request.agent_key for request in invoker.requests] == [
        "researcher",
        "writer",
    ]
    event_types = [
        event.type for event in await repository.events(waiting.run_id)
    ]
    resumed_index = event_types.index(EventType.AGENT_INVOCATION_RESUMED)
    completed_index = event_types.index(EventType.AGENT_INVOCATION_COMPLETED)
    assert event_types[resumed_index + 1] is EventType.FLOW_RESUMED
    assert resumed_index < completed_index


@pytest.mark.asyncio
async def test_sequential_flow_propagates_agent_failure_and_stops() -> None:
    strategy, invoker, _ = make_strategy(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.FAILED,
                    error="source unavailable",
                ),
            ),
            "writer": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="must not run",
                ),
            ),
        }
    )

    result = await strategy.run(make_flow(), FlowInput(prompt="Research."))

    assert result.status is RunStatus.FAILED
    assert result.error == "source unavailable"
    assert [request.agent_key for request in invoker.requests] == ["researcher"]


@pytest.mark.asyncio
async def test_sequential_flow_turns_invoker_exception_into_persisted_failure() -> None:
    strategy, _, repository = make_strategy({"researcher": ()})

    result = await strategy.run(make_flow(), FlowInput(prompt="Research."))

    assert result.status is RunStatus.FAILED
    assert "ScriptedAgentInvokerExhaustedError" in (result.error or "")
    state = await repository.get(result.run_id)
    assert state.invocations[0].status is RunStatus.FAILED
    assert [event.type for event in await repository.events(result.run_id)][-2:] == [
        EventType.AGENT_INVOCATION_FAILED,
        EventType.FLOW_FAILED,
    ]


@pytest.mark.asyncio
async def test_sequential_flow_enforces_flow_invocation_limit() -> None:
    strategy, invoker, _ = make_strategy(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="Research.",
                ),
            ),
            "writer": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="must not run",
                ),
            ),
        }
    )

    result = await strategy.run(
        make_flow(max_invocations=1),
        FlowInput(prompt="Research."),
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert result.invocation_count == 1
    assert [request.agent_key for request in invoker.requests] == ["researcher"]


@pytest.mark.asyncio
async def test_sequential_flow_rejects_incompatible_definition() -> None:
    strategy, _, _ = make_strategy({})

    with pytest.raises(UnsupportedFlowStrategyError, match="cannot execute"):
        await strategy.run(
            make_flow(strategy="router"),
            FlowInput(prompt="Route this."),
        )


@pytest.mark.asyncio
async def test_sequential_flow_persists_interruption_when_task_is_cancelled() -> None:
    repository = InMemoryFlowRepository()
    invoker = BlockingInvoker()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=invoker,
    )
    run_id = uuid4()
    task = asyncio.create_task(
        strategy.run(
            make_flow(),
            FlowInput(prompt="Research."),
            run_id=run_id,
        )
    )
    await invoker.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    state = await repository.get(run_id)
    assert state.status is RunStatus.INTERRUPTED
    assert state.invocations[0].status is RunStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_sequential_flow_rejects_invalid_invoker_result_identity() -> None:
    repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=MismatchedResultInvoker(),
    )

    result = await strategy.run(make_flow(), FlowInput(prompt="Research."))

    assert result.status is RunStatus.FAILED
    assert "invalid result" in (result.error or "")
    state = await repository.get(result.run_id)
    assert state.invocations[0].status is RunStatus.FAILED
