import asyncio
import json
from collections.abc import Sequence
from uuid import UUID, uuid4

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelRequest,
    ModelResponse,
    RunHandle,
    RunStatus,
    RuntimeCheckpoint,
    TokenUsage,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.stores import CheckpointNotFoundError
from base_agent.testing import FakeModel


@tool
async def ask_user(question: str) -> WaitForInput:
    """Ask the user for information required to continue."""
    return WaitForInput(prompt=question, metadata={"kind": "clarification"})


def waiting_response(call_id: str, question: str) -> ModelResponse:
    return ModelResponse(
        tool_calls=(
            ToolCall(
                id=call_id,
                name="ask_user",
                arguments={"question": question},
            ),
        ),
        usage=TokenUsage(input_tokens=3, output_tokens=1),
    )


def make_agent(responses: Sequence[ModelResponse]) -> tuple[Agent, FakeModel]:
    model = FakeModel(responses)
    agent = Agent(
        profile=AgentProfile(
            id="interactive",
            instructions="Ask when required.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )
    return agent, model


@pytest.mark.asyncio
async def test_tool_can_suspend_checkpoint_and_resume_the_same_run() -> None:
    agent, model = make_agent(
        [
            waiting_response("call-1", "Which region?"),
            ModelResponse(
                content="Using APAC.",
                usage=TokenUsage(input_tokens=2, output_tokens=1),
            ),
        ]
    )
    run_id = uuid4()

    waiting = await agent.run("Build the report", run_id=run_id)

    assert waiting.status is AgentResultStatus.WAITING
    assert waiting.metadata["run_id"] == str(run_id)
    assert waiting.metadata["pending_input"] == {
        "tool_call_id": "call-1",
        "tool_name": "ask_user",
        "prompt": "Which region?",
        "metadata": {"kind": "clarification"},
    }
    stored = await agent.get_run(run_id)
    assert stored.status is RunStatus.WAITING
    assert stored.metadata["pending_input"]["prompt"] == "Which region?"
    checkpoint = await agent.checkpoint_store.load(run_id)
    restored = RuntimeCheckpoint.model_validate_json(checkpoint.model_dump_json())
    assert restored.run_id == run_id
    assert restored.step_count == 1
    assert restored.pending_input.tool_call_id == "call-1"

    completed = await agent.resume(run_id, "APAC")

    assert completed.status is AgentResultStatus.COMPLETED
    assert completed.output == "Using APAC."
    assert completed.usage.total_tokens == 7
    assert completed.metadata["steps"] == 2
    assert completed.metadata["pending_input"] is None
    assert (await agent.get_run(run_id)).status is RunStatus.COMPLETED
    with pytest.raises(CheckpointNotFoundError):
        await agent.checkpoint_store.load(run_id)

    resumed_request = model.requests[1]
    tool_message = resumed_request.messages[-1]
    assert tool_message.tool_call_id == "call-1"
    assert json.loads(tool_message.content or "")["data"] == {"input": "APAC"}
    assert [event.type for event in await agent.events(run_id)] == [
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.TOOL_REQUESTED,
        EventType.TOOL_STARTED,
        EventType.TOOL_WAITING,
        EventType.RUN_WAITING,
        EventType.INPUT_RECEIVED,
        EventType.RUN_RESUMED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_run_can_wait_more_than_once_before_completing() -> None:
    agent, _ = make_agent(
        [
            waiting_response("call-1", "Region?"),
            waiting_response("call-2", "Year?"),
            ModelResponse(content="APAC 2026"),
        ]
    )
    first = await agent.run("Build")
    run_id = uuid_from(first.metadata["run_id"])

    second = await agent.resume(run_id, "APAC")
    third = await agent.resume(run_id, "2026")

    assert second.status is AgentResultStatus.WAITING
    assert second.metadata["pending_input"]["tool_call_id"] == "call-2"
    assert third.status is AgentResultStatus.COMPLETED
    assert third.output == "APAC 2026"
    assert third.metadata["steps"] == 3


class BlockingResumeModel:
    name = "blocking-resume"

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.resumed = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if len(self.requests) == 1:
            return waiting_response("call-1", "Continue?")
        self.resumed.set()
        await self.release.wait()
        return ModelResponse(content="continued")


@pytest.mark.asyncio
async def test_concurrent_resume_is_rejected() -> None:
    model = BlockingResumeModel()
    agent = Agent(
        profile=AgentProfile(
            id="interactive",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )
    waiting = await agent.run("Begin")
    run_id = uuid_from(waiting.metadata["run_id"])
    first_resume = asyncio.create_task(agent.resume(run_id, "yes"))
    await model.resumed.wait()

    with pytest.raises(ValueError, match="not waiting"):
        await agent.resume(run_id, "duplicate")

    model.release.set()
    assert (await first_resume).status is AgentResultStatus.COMPLETED


@pytest.mark.asyncio
async def test_invalid_resume_input_does_not_consume_checkpoint() -> None:
    agent, _ = make_agent(
        [waiting_response("call-1", "Region?"), ModelResponse(content="done")]
    )
    waiting = await agent.run("Begin")
    run_id = uuid_from(waiting.metadata["run_id"])

    with pytest.raises(ValueError, match="must not be empty"):
        await agent.resume(run_id, " ")

    assert (await agent.checkpoint_store.load(run_id)).pending_input.prompt == "Region?"
    assert (await agent.resume(run_id, "APAC")).status is AgentResultStatus.COMPLETED


@pytest.mark.asyncio
async def test_event_stream_continues_from_waiting_cursor_after_resume() -> None:
    agent, _ = make_agent(
        [waiting_response("call-1", "Region?"), ModelResponse(content="done")]
    )
    handle = await agent.start("Begin")
    waiting = await handle.result()
    waiting_events = await handle.events()
    waiting_sequence = waiting_events[-1].sequence
    collecting = asyncio.create_task(
        collect_types_after(handle, after_sequence=waiting_sequence)
    )
    await asyncio.sleep(0)

    completed = await agent.resume(handle.run_id, "APAC")
    resumed_types = await collecting

    assert waiting.status is AgentResultStatus.WAITING
    assert completed.status is AgentResultStatus.COMPLETED
    assert resumed_types == [
        EventType.INPUT_RECEIVED,
        EventType.RUN_RESUMED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_cancelling_waiting_run_finalizes_and_removes_checkpoint() -> None:
    agent, _ = make_agent([waiting_response("call-1", "Continue?")])
    waiting = await agent.run("Begin")
    run_id = uuid_from(waiting.metadata["run_id"])

    cancelled = await agent.cancel(run_id)

    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert (await agent.events(run_id))[-1].type is EventType.RUN_CANCELLED
    with pytest.raises(CheckpointNotFoundError):
        await agent.checkpoint_store.load(run_id)
    with pytest.raises(ValueError, match="not waiting"):
        await agent.resume(run_id, "too late")


def uuid_from(value: object) -> UUID:
    return UUID(str(value))


async def collect_types_after(
    handle: RunHandle, *, after_sequence: int
) -> list[EventType]:
    return [event.type async for event in handle.stream(after_sequence=after_sequence)]
