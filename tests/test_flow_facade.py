from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    Flow,
    MessageRole,
    ModelResponse,
    RunStatus,
    ToolCall,
    ToolConfirmation,
    ToolConfirmationDecision,
    ToolConfirmationMode,
    ToolSideEffectMode,
    WaitForInput,
    tool,
)
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_flow_sequence_runs_definition_backed_agents_without_runtime_wiring() -> None:
    researcher_model = FakeModel([ModelResponse(content="Research complete.")])
    writer_model = FakeModel([ModelResponse(content="Final report.")])
    researcher = Agent(
        definition=AgentDefinition(
            id="facade-researcher",
            version="1.0.0",
            instructions="Research.",
        ),
        model=researcher_model,
    )
    writer = Agent(
        definition=AgentDefinition(
            id="facade-writer",
            version="1.0.0",
            instructions="Write.",
        ),
        model=writer_model,
    )
    flow = Flow.sequence(
        {
            "researcher": researcher,
            "writer": writer,
        },
        id="report-flow",
    )

    run = await flow.run("Prepare the report.")

    assert run.status is RunStatus.COMPLETED
    assert run.output == "Final report."
    assert run.result.invocation_count == 2
    writer_prompt = writer_model.requests[0].messages[-1]
    assert writer_prompt.role is MessageRole.USER
    assert "Research complete." in (writer_prompt.content or "")
    assert [event.type.value for event in await run.events()][-1] == (
        "flow.completed"
    )


@pytest.mark.asyncio
async def test_flow_run_resumes_pending_input_directly() -> None:
    @tool
    async def ask_user(question: str) -> WaitForInput:
        return WaitForInput(prompt=question)

    agent = Agent(
        definition=AgentDefinition(
            id="facade-interactive-agent",
            version="1.0.0",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="question-call",
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
    flow = Flow.sequence({"worker": agent}, id="interactive-flow")

    waiting = await flow.run("Build report.")
    completed = await waiting.resume("APAC")

    assert waiting.waiting
    assert waiting.pending_input is not None
    assert waiting.pending_input.prompt == "Which region?"
    assert completed.status is RunStatus.COMPLETED
    assert completed.output == "Using APAC."


@pytest.mark.asyncio
async def test_flow_run_confirms_original_tool_call() -> None:
    executed = False

    @tool(
        side_effect=ToolSideEffectMode.UNSAFE,
        confirmation=ToolConfirmationMode.REQUIRED,
    )
    async def publish() -> str:
        nonlocal executed
        executed = True
        return "published"

    agent = Agent(
        definition=AgentDefinition(
            id="facade-publisher",
            version="1.0.0",
            instructions="Publish after confirmation.",
            tools=("publish",),
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="publish-call",
                            name="publish",
                            arguments={},
                        ),
                    )
                ),
                ModelResponse(content="Published."),
            ]
        ),
        tools=(publish,),
    )
    flow = Flow.sequence({"publisher": agent}, id="publish-flow")
    waiting = await flow.run("Publish.")
    assert waiting.pending_input is not None
    request = waiting.pending_input.metadata["request"]

    completed = await waiting.confirm(
        ToolConfirmation(
            request_id=UUID(request["id"]),
            decision=ToolConfirmationDecision.APPROVE,
            subject_id="operator-1",
            reason_code="approved",
        )
    )

    assert completed.status is RunStatus.COMPLETED
    assert executed
