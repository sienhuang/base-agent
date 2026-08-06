import asyncio

import pytest

from base_agent import (
    AgentDefinition,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentInvocationResume,
    AgentInvoker,
    AgentResultStatus,
    EventType,
    FlowAgent,
    FlowBudget,
    FlowDefinition,
    FlowInput,
    FlowLifecycle,
    InMemoryFlowRepository,
    PendingInput,
    RunStatus,
    SequentialFlowStrategy,
    TokenUsage,
)
from base_agent.testing import ScriptedAgentInvoker, ScriptedAgentOutcome


class SlowInvoker:
    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        await asyncio.sleep(60)
        raise AssertionError("Flow timeout should cancel the invocation")

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult:
        raise AssertionError("resume is not expected")


def make_flow(
    *,
    budget: FlowBudget,
    agent_count: int = 2,
) -> FlowDefinition:
    bindings = (
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
    )
    return FlowDefinition(
        id="budgeted-flow",
        version="1.0.0",
        agents=bindings[:agent_count],
        strategy="sequential",
        budget=budget,
    )


def make_strategy(
    invoker: AgentInvoker,
) -> tuple[SequentialFlowStrategy, InMemoryFlowRepository]:
    repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(repository),
        invoker=invoker,
    )
    return strategy, repository


@pytest.mark.asyncio
async def test_flow_stops_when_agent_result_exceeds_total_token_budget() -> None:
    invoker = ScriptedAgentInvoker(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="research",
                    usage=TokenUsage(input_tokens=4, output_tokens=2),
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
    strategy, repository = make_strategy(invoker)

    result = await strategy.run(
        make_flow(budget=FlowBudget(max_total_tokens=5)),
        FlowInput(prompt="Work."),
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert result.usage.total_tokens == 6
    assert [request.agent_key for request in invoker.requests] == ["researcher"]
    terminal = (await repository.events(result.run_id))[-1]
    assert terminal.type is EventType.FLOW_LIMIT_REACHED
    assert terminal.data["budget"] == {
        "kind": "total_tokens",
        "limit": 5.0,
        "actual": 6.0,
    }


@pytest.mark.asyncio
async def test_exact_budget_allows_completion_but_not_another_invocation() -> None:
    exact_outcome = ScriptedAgentOutcome(
        status=AgentResultStatus.COMPLETED,
        output="research",
        usage=TokenUsage(input_tokens=4, output_tokens=1),
    )
    one_agent = ScriptedAgentInvoker({"researcher": (exact_outcome,)})
    one_strategy, _ = make_strategy(one_agent)

    completed = await one_strategy.run(
        make_flow(
            budget=FlowBudget(max_total_tokens=5),
            agent_count=1,
        ),
        FlowInput(prompt="Work."),
    )

    assert completed.status is RunStatus.COMPLETED

    two_agents = ScriptedAgentInvoker(
        {
            "researcher": (exact_outcome,),
            "writer": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="must not run",
                ),
            ),
        }
    )
    two_strategy, _ = make_strategy(two_agents)
    limited = await two_strategy.run(
        make_flow(budget=FlowBudget(max_total_tokens=5)),
        FlowInput(prompt="Work."),
    )

    assert limited.status is RunStatus.LIMIT_REACHED
    assert [request.agent_key for request in two_agents.requests] == ["researcher"]


@pytest.mark.asyncio
async def test_flow_aggregates_model_and_tool_call_budgets() -> None:
    invoker = ScriptedAgentInvoker(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="research",
                    metadata={"model_calls": 2, "tool_calls": 3},
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
    strategy, _ = make_strategy(invoker)

    result = await strategy.run(
        make_flow(
            budget=FlowBudget(
                max_model_calls=1,
                max_tool_calls=10,
            )
        ),
        FlowInput(prompt="Work."),
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert result.model_call_count == 2
    assert result.tool_call_count == 3
    assert "model_calls" in (result.error or "")


@pytest.mark.parametrize(
    ("budget", "usage", "metadata", "expected_kind"),
    [
        (
            FlowBudget(max_input_tokens=3),
            TokenUsage(input_tokens=4),
            {},
            "input_tokens",
        ),
        (
            FlowBudget(max_output_tokens=2),
            TokenUsage(output_tokens=3),
            {},
            "output_tokens",
        ),
        (
            FlowBudget(max_model_calls=1),
            TokenUsage(),
            {"model_calls": 2},
            "model_calls",
        ),
        (
            FlowBudget(max_tool_calls=1),
            TokenUsage(),
            {"tool_calls": 2},
            "tool_calls",
        ),
    ],
)
@pytest.mark.asyncio
async def test_flow_enforces_each_cumulative_budget_dimension(
    budget: FlowBudget,
    usage: TokenUsage,
    metadata: dict[str, int],
    expected_kind: str,
) -> None:
    invoker = ScriptedAgentInvoker(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="result",
                    usage=usage,
                    metadata=metadata,
                ),
            ),
        }
    )
    strategy, repository = make_strategy(invoker)

    result = await strategy.run(
        make_flow(budget=budget, agent_count=1),
        FlowInput(prompt="Work."),
    )

    assert result.status is RunStatus.LIMIT_REACHED
    terminal = (await repository.events(result.run_id))[-1]
    budget_event = terminal.data["budget"]
    assert isinstance(budget_event, dict)
    assert budget_event["kind"] == expected_kind


@pytest.mark.asyncio
async def test_waiting_flow_checks_cumulative_budget_before_resume_transport() -> None:
    invoker = ScriptedAgentInvoker(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.WAITING,
                    usage=TokenUsage(input_tokens=4, output_tokens=1),
                    pending_input=PendingInput(
                        tool_call_id="call-1",
                        tool_name="ask_user",
                        prompt="Continue?",
                    ),
                ),
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="must not resume",
                ),
            ),
        }
    )
    strategy, repository = make_strategy(invoker)
    definition = make_flow(
        budget=FlowBudget(max_total_tokens=5),
        agent_count=1,
    )
    waiting = await strategy.run(definition, FlowInput(prompt="Work."))

    limited = await strategy.resume(definition, waiting.run_id, "yes")

    assert waiting.status is RunStatus.WAITING
    assert limited.status is RunStatus.LIMIT_REACHED
    assert invoker.resumes == ()
    assert len(invoker.cancellations) == 1
    state = await repository.get(waiting.run_id)
    assert state.invocations[0].status is RunStatus.LIMIT_REACHED


@pytest.mark.asyncio
async def test_flow_wall_clock_budget_cancels_slow_transport() -> None:
    strategy, repository = make_strategy(SlowInvoker())

    result = await strategy.run(
        make_flow(
            budget=FlowBudget(timeout_seconds=0.01),
            agent_count=1,
        ),
        FlowInput(prompt="Work."),
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert "timeout_seconds" in (result.error or "")
    state = await repository.get(result.run_id)
    assert state.invocations[0].status is RunStatus.LIMIT_REACHED


def test_legacy_invocation_limit_normalizes_into_canonical_budget() -> None:
    legacy = FlowDefinition(
        id="legacy-flow",
        version="1.0.0",
        agents=make_flow(
            budget=FlowBudget(),
            agent_count=1,
        ).agents,
        strategy="sequential",
        max_invocations=2,
    )
    canonical = legacy.model_copy(
        update={"budget": FlowBudget(max_invocations=2)}
    )

    assert legacy.budget == FlowBudget(max_invocations=2)
    assert legacy.max_invocations == 2
    assert legacy.fingerprint == canonical.fingerprint
    assert "max_invocations" not in legacy.model_dump()
