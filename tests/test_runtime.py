import pytest
from pydantic import ValidationError

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    AgentRuntime,
    ExecutionState,
    InvalidStateTransitionError,
    MessageRole,
    ModelResponse,
    RuntimeContext,
    TokenUsage,
    ToolCall,
)
from base_agent.runtime import RuntimeStateMachine
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_agent_completes_a_text_only_run() -> None:
    model = FakeModel(
        [ModelResponse(content="Hello!", usage=TokenUsage(input_tokens=4, output_tokens=2))]
    )
    profile = AgentProfile(id="assistant", instructions="Be helpful.", model="test-model")
    agent = Agent(profile=profile, model=model)

    result = await agent.run("Hello")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "Hello!"
    assert result.usage.total_tokens == 6
    assert result.metadata["steps"] == 1
    assert result.metadata["provider"] == "fake-model"
    assert [message.role for message in result.messages] == [
        MessageRole.SYSTEM,
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert model.requests[0].model == "test-model"
    assert model.requests[0].messages[0].content == "Be helpful."


@pytest.mark.asyncio
async def test_repeated_runs_do_not_share_context_or_messages() -> None:
    model = FakeModel([ModelResponse(content="first"), ModelResponse(content="second")])
    agent = Agent(
        profile=AgentProfile(id="assistant", instructions="Be helpful."),
        model=model,
    )

    first = await agent.run("one")
    second = await agent.run("two")

    assert first.metadata["run_id"] != second.metadata["run_id"]
    assert first.messages[1].content == "one"
    assert second.messages[1].content == "two"
    assert len(model.requests[0].messages) == len(model.requests[1].messages) == 2


@pytest.mark.asyncio
async def test_provider_failure_becomes_a_typed_failed_result() -> None:
    model = FakeModel([])
    agent = Agent(
        profile=AgentProfile(id="assistant", instructions="Be helpful."),
        model=model,
    )

    result = await agent.run("Hello")

    assert result.status is AgentResultStatus.FAILED
    assert result.output is None
    assert "FakeModel has no scripted responses" in (result.error or "")


@pytest.mark.asyncio
async def test_tool_outside_profile_is_denied_and_returned_to_model_for_recovery() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="call-1", name="weather.get", arguments={}),)
            ),
            ModelResponse(content="I cannot access that tool."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="assistant", instructions="Be helpful."),
        model=model,
    )

    result = await agent.run("Weather?")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "I cannot access that tool."
    assert "tool_not_allowed" in (model.requests[1].messages[-1].content or "")


@pytest.mark.asyncio
async def test_context_at_step_limit_returns_typed_limit_result() -> None:
    profile = AgentProfile(id="assistant", instructions="Be helpful.", max_steps=1)
    runtime = AgentRuntime()
    context = runtime.create_context(profile, "Hello")
    context.state_machine.transition_to(ExecutionState.RUNNING)
    context.step_count = 1

    result = await runtime.execute(context, FakeModel([ModelResponse(content="unused")]))

    assert result.status is AgentResultStatus.LIMIT_REACHED
    assert result.error == "maximum model steps reached (1)"
    assert result.metadata["steps"] == 1


def test_state_machine_rejects_invalid_and_terminal_transitions() -> None:
    state_machine = RuntimeStateMachine()

    with pytest.raises(InvalidStateTransitionError, match="created -> completed"):
        state_machine.transition_to(ExecutionState.COMPLETED)

    state_machine.transition_to(ExecutionState.RUNNING)
    state_machine.transition_to(ExecutionState.COMPLETED)

    with pytest.raises(InvalidStateTransitionError, match="completed -> running"):
        state_machine.transition_to(ExecutionState.RUNNING)


def test_waiting_state_can_resume_or_be_cancelled() -> None:
    resumable = RuntimeStateMachine()
    resumable.transition_to(ExecutionState.RUNNING)
    resumable.transition_to(ExecutionState.WAITING)

    assert resumable.is_terminal is False
    resumable.transition_to(ExecutionState.RUNNING)
    resumable.transition_to(ExecutionState.WAITING)
    resumable.transition_to(ExecutionState.CANCELLED)
    assert resumable.is_terminal is True


@pytest.mark.parametrize("prompt", ["", "   "])
def test_runtime_rejects_empty_prompts(prompt: str) -> None:
    runtime = AgentRuntime()
    profile = AgentProfile(id="assistant", instructions="Be helpful.")

    with pytest.raises(ValueError, match="prompt must not be empty"):
        runtime.create_context(profile, prompt)


def test_profile_rejects_invalid_limits_and_identifiers() -> None:
    with pytest.raises(ValidationError):
        AgentProfile(id="contains spaces", instructions="Be helpful.")
    with pytest.raises(ValidationError):
        AgentProfile(id="assistant", instructions="Be helpful.", max_steps=0)


def test_runtime_context_reports_a_typed_state() -> None:
    context = RuntimeContext(
        profile=AgentProfile(id="assistant", instructions="Be helpful."),
        messages=[],
    )

    assert context.state is ExecutionState.CREATED
