from uuid import uuid4

import pytest

from base_agent import (
    AgentDefinition,
    AgentResultStatus,
    EventType,
    FlowAgent,
    FlowBudget,
    FlowDefinition,
    PendingInput,
    RunStatus,
    TokenUsage,
)
from base_agent.testing import (
    FlowTestHarness,
    FlowTestRun,
    ScriptedAgentOutcome,
)


def make_flow(*, budget: FlowBudget | None = None) -> FlowDefinition:
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
        budget=budget or FlowBudget(),
    )


@pytest.mark.asyncio
async def test_flow_test_harness_captures_order_handoff_and_events() -> None:
    harness = FlowTestHarness(
        make_flow(),
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
        },
    )

    episode = await harness.run("Research harness engineering.")

    assert episode.result.status is RunStatus.COMPLETED
    assert episode.result.output == "Final report."
    assert episode.agent_keys == ("researcher", "writer")
    assert len(episode.requests_for("writer")) == 1
    handoff = episode.requests_for("writer")[0].input.handoff
    assert handoff is not None
    assert handoff.summary == "Three sources support the conclusion."
    assert handoff.data == {"confidence": 0.9}
    assert episode.event_types[-1] is EventType.FLOW_COMPLETED
    assert episode.state == episode.state.model_validate_json(
        episode.state.model_dump_json()
    )
    assert FlowTestRun.model_validate_json(episode.model_dump_json()) == episode


@pytest.mark.asyncio
async def test_flow_test_harness_resume_returns_cumulative_evidence() -> None:
    pending = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Which region?",
    )
    harness = FlowTestHarness(
        make_flow(),
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
                ),
            ),
        },
    )

    waiting = await harness.run("Research the market.")
    completed = await harness.resume(waiting.result.run_id, "APAC")

    assert waiting.result.status is RunStatus.WAITING
    assert waiting.result.pending_input == pending
    assert completed.result.status is RunStatus.COMPLETED
    assert completed.result.run_id == waiting.result.run_id
    assert len(completed.requests) == 2
    assert len(completed.resumes) == 1
    assert completed.resumes[0].user_input == "APAC"
    assert EventType.FLOW_WAITING in completed.event_types
    assert EventType.FLOW_RESUMED in completed.event_types


@pytest.mark.asyncio
async def test_flow_test_harness_captures_budget_and_cancel_evidence() -> None:
    budget_harness = FlowTestHarness(
        make_flow(budget=FlowBudget(max_total_tokens=3)),
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="too expensive",
                    usage=TokenUsage(input_tokens=3, output_tokens=1),
                ),
            ),
        },
    )

    limited = await budget_harness.run("Work.")

    assert limited.result.status is RunStatus.LIMIT_REACHED
    assert limited.events[-1].data["budget"] == {
        "kind": "total_tokens",
        "limit": 3.0,
        "actual": 4.0,
    }

    pending = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Continue?",
    )
    cancel_harness = FlowTestHarness(
        make_flow(),
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.WAITING,
                    pending_input=pending,
                ),
            ),
        },
    )
    waiting = await cancel_harness.run("Work.")
    cancelled = await cancel_harness.cancel(
        waiting.result.run_id,
        reason="test requested cancellation",
    )

    assert cancelled.result.status is RunStatus.CANCELLED
    assert len(cancelled.cancellations) == 1
    assert cancelled.cancellations[0].reason == "test requested cancellation"
    assert cancelled.invocations[0].status is RunStatus.CANCELLED


@pytest.mark.asyncio
async def test_flow_test_harness_isolates_evidence_between_runs() -> None:
    harness = FlowTestHarness(
        make_flow(),
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="first research",
                ),
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="second research",
                ),
            ),
            "writer": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="first report",
                ),
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="second report",
                ),
            ),
        },
    )

    first = await harness.run("one")
    second = await harness.run("two")

    assert first.result.output == "first report"
    assert second.result.output == "second report"
    assert len(first.requests) == 2
    assert len(second.requests) == 2
    assert {request.flow_run_id for request in first.requests} == {
        first.result.run_id
    }
    assert {request.flow_run_id for request in second.requests} == {
        second.result.run_id
    }


@pytest.mark.asyncio
async def test_flow_test_harness_rejects_unknown_or_untracked_runs() -> None:
    with pytest.raises(ValueError, match="unknown Agent keys"):
        FlowTestHarness(
            make_flow(),
            {
                "reviewer": (
                    ScriptedAgentOutcome(
                        status=AgentResultStatus.COMPLETED,
                        output="review",
                    ),
                )
            },
        )

    harness = FlowTestHarness(make_flow(), {})
    with pytest.raises(ValueError, match="not tracked"):
        await harness.resume(uuid4(), "input")
