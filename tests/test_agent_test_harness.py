import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelResponse,
    RunStatus,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.testing import AgentTestHarness, FakeModel


@pytest.mark.asyncio
async def test_agent_test_harness_captures_complete_runtime_evidence() -> None:
    @tool(permissions=frozenset({"weather:read"}))
    async def weather(city: str) -> dict[str, str]:
        return {"city": city, "condition": "sunny"}

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="weather",
                        arguments={"city": "Shanghai"},
                    ),
                )
            ),
            ModelResponse(content="Shanghai is sunny."),
        ]
    )
    harness = AgentTestHarness(
        Agent(
            profile=AgentProfile(
                id="weather-agent",
                instructions="Use the weather Tool.",
                tools=("weather",),
                permissions=frozenset({"weather:read"}),
            ),
            model=model,
            tools=(weather,),
        )
    )

    episode = await harness.run("What is the weather?")

    assert episode.result.status is AgentResultStatus.COMPLETED
    assert episode.result.output == "Shanghai is sunny."
    assert episode.run.status is RunStatus.COMPLETED
    assert len(episode.model_requests) == 2
    assert episode.event_types == (
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_COMPLETED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    )


@pytest.mark.asyncio
async def test_agent_test_harness_captures_wait_and_resume_as_one_run() -> None:
    @tool
    async def ask_user(question: str) -> WaitForInput:
        return WaitForInput(prompt=question)

    harness = AgentTestHarness(
        Agent(
            profile=AgentProfile(
                id="interactive-agent",
                instructions="Ask for missing information.",
                tools=("ask_user",),
            ),
            model=FakeModel(
                [
                    ModelResponse(
                        tool_calls=(
                            ToolCall(
                                id="call-1",
                                name="ask_user",
                                arguments={"question": "Which region?"},
                            ),
                        )
                    ),
                    ModelResponse(content="Using APAC."),
                ]
            ),
            tools=(ask_user,),
        )
    )

    waiting = await harness.run("Build the report.")
    completed = await harness.resume(waiting.run.id, "APAC")

    assert waiting.result.status is AgentResultStatus.WAITING
    assert waiting.run.status is RunStatus.WAITING
    assert completed.result.status is AgentResultStatus.COMPLETED
    assert completed.run.id == waiting.run.id
    assert len(completed.model_requests) == 2
    assert EventType.RUN_WAITING in completed.event_types
    assert EventType.RUN_RESUMED in completed.event_types
    assert completed.event_types[-1] is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_agent_test_harness_isolates_model_requests_between_runs() -> None:
    harness = AgentTestHarness(
        Agent(
            profile=AgentProfile(id="repeatable-agent", instructions="Reply."),
            model=FakeModel(
                [
                    ModelResponse(content="first"),
                    ModelResponse(content="second"),
                ]
            ),
        )
    )

    first = await harness.run("one")
    second = await harness.run("two")

    assert len(first.model_requests) == 1
    assert first.model_requests[0].messages[-1].content == "one"
    assert len(second.model_requests) == 1
    assert second.model_requests[0].messages[-1].content == "two"


def test_agent_test_harness_rejects_non_deterministic_model() -> None:
    class UnscriptedModel:
        name = "unscripted"

        async def complete(self, request: object) -> ModelResponse:
            del request
            return ModelResponse(content="unexpected")

    agent = Agent(
        profile=AgentProfile(id="unsafe-test-agent", instructions="Reply."),
        model=UnscriptedModel(),
    )

    with pytest.raises(TypeError, match="requires an Agent using FakeModel"):
        AgentTestHarness(agent)
