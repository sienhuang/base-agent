import json

from base_agent import (
    Agent,
    AgentDefinition,
    PendingInputKind,
    RunStatus,
    WaitForInput,
    interaction_tools,
)
from base_agent.models import ModelResponse, ToolCall
from base_agent.testing import FakeModel
from base_agent.tools import ToolContext


async def test_interaction_tool_exposes_the_standard_ask_user_contract() -> None:
    (ask_user,) = interaction_tools()

    assert ask_user.spec.name == "ask_user"
    assert ask_user.permissions == frozenset({"interaction:ask"})
    assert ask_user.spec.input_schema["required"] == ["question"]
    assert "instead of asking the user in a normal assistant message" in (
        ask_user.spec.description
    )
    assert "If an answer is incomplete" in ask_user.spec.description

    outcome = await ask_user.invoke(
        {
            "question": "Which region?",
            "metadata": {"field": "region"},
        },
        ToolContext(
            run_id="run-1",
            agent_id="interactive-agent",
            tool_call_id="ask-1",
        ),
    )

    assert outcome == WaitForInput("Which region?", {"field": "region"})


async def test_model_can_call_ask_user_then_resume_the_same_run() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={
                            "question": "Which region?",
                            "metadata": {"field": "region"},
                        },
                    ),
                )
            ),
            ModelResponse(content="Building the APAC report."),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="interactive-agent",
            version="1.0.0",
            instructions="Ask the user when required information is missing.",
            tools=("ask_user",),
            permissions=frozenset({"interaction:ask"}),
            max_model_turns=2,
        ),
        model=model,
        tools=interaction_tools(),
    )

    waiting = await agent.run("Build a regional report")

    assert waiting.status is RunStatus.WAITING
    assert waiting.pending_input is not None
    assert waiting.pending_input.kind is PendingInputKind.TOOL_INPUT
    assert waiting.pending_input.tool_name == "ask_user"
    assert waiting.pending_input.prompt == "Which region?"
    assert waiting.pending_input.metadata == {"field": "region"}
    for event in waiting.events:
        json.dumps(dict(event.payload), allow_nan=False)

    completed = await agent.resume(waiting.run_id, "APAC")

    assert completed.status is RunStatus.SUCCEEDED
    assert completed.run_id == waiting.run_id
    assert completed.text == "Building the APAC report."
    assert completed.tool_results[-1].data == {"input": "APAC"}
