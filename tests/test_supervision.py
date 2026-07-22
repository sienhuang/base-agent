from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelResponse,
    RunStatus,
    Supervisor,
    ToolCall,
    tool,
)
from base_agent.supervision import CompositeSupervisor, ExecutionBudget
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_model_and_tool_budgets_are_independent() -> None:
    executions: list[str] = []

    @tool
    async def record(value: str) -> str:
        executions.append(value)
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-1", name="record", arguments={"value": "first"}),
                    ToolCall(id="call-2", name="record", arguments={"value": "second"}),
                )
            ),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("record",),
            max_steps=5,
            max_tool_calls=1,
        ),
        model=model,
        tools=[record],
    )

    result = await agent.run("record")
    run_id = _uuid(result.metadata["run_id"])
    run = await agent.get_run(run_id)
    events = await agent.events(run_id)

    assert result.status is AgentResultStatus.LIMIT_REACHED
    assert result.error == "maximum tool calls reached (1)"
    assert executions == ["first"]
    assert len(model.requests) == 1
    assert run.status is RunStatus.LIMIT_REACHED
    assert run.step_count == 1
    assert run.tool_call_count == 1
    intervention = next(
        event for event in events if event.type is EventType.SUPERVISOR_INTERVENED
    )
    assert intervention.data["policy"] == "execution-budget"
    assert intervention.data["metadata"]["kind"] == "tool_calls"


@pytest.mark.asyncio
async def test_duplicate_tool_call_is_redirected_without_reexecution() -> None:
    executions = 0

    @tool
    async def lookup(query: str) -> str:
        nonlocal executions
        executions += 1
        return "same observation"

    repeated_call = ToolCall(id="call-1", name="lookup", arguments={"query": "same"})
    model = FakeModel(
        [
            ModelResponse(tool_calls=(repeated_call,)),
            ModelResponse(
                tool_calls=(repeated_call.model_copy(update={"id": "call-2"}),)
            ),
            ModelResponse(content="changed strategy"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("lookup",),
            duplicate_tool_call_threshold=2,
        ),
        model=model,
        tools=[lookup],
    )

    result = await agent.run("lookup")
    events = await agent.events(_uuid(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "changed strategy"
    assert executions == 1
    assert result.metadata["tool_calls"] == 2
    assert "supervisor_intervention" in (model.requests[2].messages[-2].content or "")
    assert "Do not repeat it again" in (model.requests[2].messages[-1].content or "")
    intervention = next(
        event for event in events if event.type is EventType.SUPERVISOR_INTERVENED
    )
    assert intervention.data["policy"] == "duplicate-tool-call-detector"
    assert intervention.data["action"] == "redirect"


@pytest.mark.asyncio
async def test_consecutive_tool_failures_trigger_no_progress_redirect() -> None:
    executions = 0

    @tool
    async def broken(value: str) -> str:
        nonlocal executions
        executions += 1
        raise RuntimeError(f"failed: {value}")

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="call-1", name="broken", arguments={"value": "a"}),)
            ),
            ModelResponse(
                tool_calls=(ToolCall(id="call-2", name="broken", arguments={"value": "b"}),)
            ),
            ModelResponse(content="used a different approach"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("broken",),
            max_consecutive_tool_failures=2,
        ),
        model=model,
        tools=[broken],
    )

    result = await agent.run("try")
    events = await agent.events(_uuid(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.COMPLETED
    assert executions == 2
    assert "Multiple tool attempts have failed" in (
        model.requests[2].messages[-1].content or ""
    )
    interventions = [
        event for event in events if event.type is EventType.SUPERVISOR_INTERVENED
    ]
    assert len(interventions) == 1
    assert interventions[0].data["policy"] == "no-progress-detector"
    assert interventions[0].data["metadata"]["count"] == 2


def test_composite_supervisor_implements_public_protocol() -> None:
    supervisor = CompositeSupervisor([ExecutionBudget()])

    assert isinstance(supervisor, Supervisor)


def _uuid(value: object) -> UUID:
    return UUID(str(value))
