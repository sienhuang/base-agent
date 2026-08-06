from uuid import uuid4

import pytest

from base_agent import (
    AgentDefinition,
    AgentInvocationInput,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentResultStatus,
    FlowAgent,
    FlowDefinition,
    FlowRunState,
    InvalidFlowTransitionError,
    PendingInput,
    RunStatus,
    TokenUsage,
)


def make_flow(*, max_invocations: int = 3) -> FlowDefinition:
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
        strategy="sequential",
        max_invocations=max_invocations,
    )


def request_for(
    state: FlowRunState,
    sequence: int,
    agent_key: str,
    definition: FlowDefinition,
) -> AgentInvocationRequest:
    agent_definition = definition.agent(agent_key)
    return AgentInvocationRequest(
        flow_run_id=state.run_id,
        sequence=sequence,
        agent_key=agent_key,
        definition_id=agent_definition.id,
        definition_version=agent_definition.version,
        definition_fingerprint=agent_definition.fingerprint,
        input=AgentInvocationInput(prompt=f"Invoke {agent_key}."),
    )


def result_for(
    request: AgentInvocationRequest,
    *,
    status: AgentResultStatus = AgentResultStatus.COMPLETED,
    output: str | None = "done",
    usage: TokenUsage | None = None,
    pending_input: PendingInput | None = None,
    error: str | None = None,
) -> AgentInvocationResult:
    return AgentInvocationResult(
        flow_run_id=request.flow_run_id,
        invocation_id=request.invocation_id,
        agent_key=request.agent_key,
        status=status,
        output=output,
        usage=usage or TokenUsage(),
        pending_input=pending_input,
        error=error,
    )


def test_flow_run_executes_invocations_and_aggregates_usage() -> None:
    definition = make_flow()
    created = FlowRunState.create(definition)
    assert created.revision == 1
    state = created.start()
    assert state.revision == 2
    research = request_for(state, 1, "researcher", definition)

    state = state.begin_invocation(research, definition=definition)
    assert state.active_invocation is not None
    assert state.active_invocation.definition_id == "research-agent"
    state = state.settle_invocation(
        result_for(
            research,
            output="research complete",
            usage=TokenUsage(input_tokens=10, output_tokens=2),
        )
    )
    writing = request_for(state, 2, "writer", definition)
    state = state.begin_invocation(writing, definition=definition)
    state = state.settle_invocation(
        result_for(
            writing,
            output="report complete",
            usage=TokenUsage(input_tokens=8, output_tokens=4),
        )
    )
    state = state.complete("final report")

    assert state.status is RunStatus.COMPLETED
    assert state.revision == 7
    assert state.output == "final report"
    assert [item.agent_key for item in state.invocations] == [
        "researcher",
        "writer",
    ]
    assert state.usage == TokenUsage(input_tokens=18, output_tokens=6)
    assert FlowRunState.model_validate_json(state.model_dump_json()) == state


def test_waiting_invocation_resumes_without_double_counting_cumulative_usage() -> None:
    definition = make_flow()
    state = FlowRunState.create(definition).start()
    request = request_for(state, 1, "researcher", definition)
    state = state.begin_invocation(request, definition=definition)
    pending = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Which region?",
    )

    state = state.settle_invocation(
        result_for(
            request,
            status=AgentResultStatus.WAITING,
            output=None,
            usage=TokenUsage(input_tokens=5, output_tokens=1),
            pending_input=pending,
        )
    )
    assert state.status is RunStatus.WAITING
    assert state.usage.total_tokens == 6

    state = state.resume_invocation(request.invocation_id)
    assert state.status is RunStatus.RUNNING
    assert state.active_invocation is not None
    assert state.active_invocation.status is RunStatus.RUNNING
    assert state.usage.total_tokens == 6
    assert FlowRunState.model_validate_json(state.model_dump_json()) == state

    state = state.settle_invocation(
        result_for(
            request,
            output="APAC research complete",
            usage=TokenUsage(input_tokens=9, output_tokens=3),
        )
    )

    assert state.status is RunStatus.RUNNING
    assert state.active_invocation is None
    assert state.usage == TokenUsage(input_tokens=9, output_tokens=3)


def test_flow_run_rejects_concurrent_or_out_of_order_invocations() -> None:
    definition = make_flow(max_invocations=1)
    state = FlowRunState.create(definition).start()
    first = request_for(state, 1, "researcher", definition)
    state = state.begin_invocation(first, definition=definition)

    with pytest.raises(InvalidFlowTransitionError, match="already has an active"):
        state.begin_invocation(
            request_for(state, 2, "writer", definition),
            definition=definition,
        )

    state = state.settle_invocation(result_for(first))
    with pytest.raises(InvalidFlowTransitionError, match="limit reached"):
        state.begin_invocation(
            request_for(state, 2, "writer", definition),
            definition=definition,
        )


def test_flow_run_rejects_mismatched_definition_and_result_identity() -> None:
    definition = make_flow()
    state = FlowRunState.create(definition).start()
    changed = definition.model_copy(update={"version": "2.0.0"})
    request = request_for(state, 1, "researcher", definition)

    with pytest.raises(ValueError, match="does not match"):
        state.begin_invocation(request, definition=changed)

    state = state.begin_invocation(request, definition=definition)
    wrong = result_for(request).model_copy(update={"invocation_id": uuid4()})
    with pytest.raises(ValueError, match="invocation_id does not match"):
        state.settle_invocation(wrong)


def test_unsuccessful_invocation_prevents_completion_and_flow_can_fail() -> None:
    definition = make_flow()
    state = FlowRunState.create(definition).start()
    request = request_for(state, 1, "researcher", definition)
    state = state.begin_invocation(request, definition=definition)
    state = state.settle_invocation(
        result_for(
            request,
            status=AgentResultStatus.FAILED,
            output=None,
            error="research failed",
        )
    )

    with pytest.raises(InvalidFlowTransitionError, match="unsuccessful"):
        state.complete("invalid")

    failed = state.fail("research stage failed")
    assert failed.status is RunStatus.FAILED
    assert failed.error == "research stage failed"


@pytest.mark.parametrize(
    ("method_name", "expected"),
    [
        ("cancel", RunStatus.CANCELLED),
        ("interrupt", RunStatus.INTERRUPTED),
        ("reach_limit", RunStatus.LIMIT_REACHED),
    ],
)
def test_flow_termination_settles_active_invocation(
    method_name: str,
    expected: RunStatus,
) -> None:
    definition = make_flow()
    state = FlowRunState.create(definition).start()
    request = request_for(state, 1, "researcher", definition)
    state = state.begin_invocation(request, definition=definition)

    terminated = getattr(state, method_name)("stopped")

    assert terminated.status is expected
    assert terminated.active_invocation is None
    assert terminated.invocations[0].status is expected
    assert terminated.invocations[0].result is not None
    assert terminated.invocations[0].result.error == "stopped"


def test_cancelling_waiting_flow_preserves_accrued_invocation_usage() -> None:
    definition = make_flow()
    state = FlowRunState.create(definition).start()
    request = request_for(state, 1, "researcher", definition)
    state = state.begin_invocation(request, definition=definition)
    state = state.settle_invocation(
        result_for(
            request,
            status=AgentResultStatus.WAITING,
            output=None,
            usage=TokenUsage(input_tokens=4, output_tokens=1),
            pending_input=PendingInput(
                tool_call_id="call-1",
                tool_name="ask_user",
                prompt="Continue?",
            ),
        )
    )

    cancelled = state.cancel()

    assert cancelled.usage == TokenUsage(input_tokens=4, output_tokens=1)
