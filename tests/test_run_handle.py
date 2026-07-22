import asyncio

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelRequest,
    ModelResponse,
    RunHandle,
)
from base_agent.testing import FakeModel


class ControlledModel:
    name = "controlled"

    def __init__(self, content: str = "done") -> None:
        self.content = content
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse(content=self.content)


async def collect_event_types(handle: RunHandle) -> list[EventType]:
    return [event.type async for event in handle.stream()]


@pytest.mark.asyncio
async def test_start_returns_live_handle_and_streams_until_completion() -> None:
    model = ControlledModel("finished")
    agent = Agent(
        profile=AgentProfile(id="background", instructions="Work."),
        model=model,
    )

    handle = await agent.start("Begin")
    await model.started.wait()
    collecting = asyncio.create_task(collect_event_types(handle))
    await asyncio.sleep(0)

    assert handle.done is False
    assert (await handle.get_run()).status.value == "running"

    model.release.set()
    result = await handle.result()
    event_types = await collecting

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "finished"
    assert handle.done is True
    assert event_types == [
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_stream_replays_only_events_after_sequence_cursor() -> None:
    agent = Agent(
        profile=AgentProfile(id="replay", instructions="Work."),
        model=FakeModel([ModelResponse(content="done")]),
    )
    handle = await agent.start("Begin")
    await handle.result()

    replayed = [event async for event in handle.stream(after_sequence=3)]

    assert [event.sequence for event in replayed] == [4, 5]
    assert [event.type for event in replayed] == [
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_handle_requests_cooperative_cancellation() -> None:
    model = ControlledModel()
    agent = Agent(
        profile=AgentProfile(id="cancel", instructions="Work."),
        model=model,
    )
    handle = await agent.start("Begin")
    await model.started.wait()

    requested = await handle.cancel()
    model.release.set()
    result = await handle.result()

    assert requested.cancel_requested is True
    assert result.status is AgentResultStatus.CANCELLED
    assert (await handle.events())[-1].type is EventType.RUN_CANCELLED


@pytest.mark.asyncio
async def test_cancelling_one_result_waiter_does_not_cancel_the_run() -> None:
    model = ControlledModel("survived")
    agent = Agent(
        profile=AgentProfile(id="shielded", instructions="Work."),
        model=model,
    )
    handle = await agent.start("Begin")
    await model.started.wait()
    waiter = asyncio.create_task(handle.result())
    waiter.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiter

    model.release.set()
    result = await handle.result()
    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "survived"


@pytest.mark.asyncio
async def test_background_runs_keep_events_and_results_isolated() -> None:
    first = Agent(
        profile=AgentProfile(id="first", instructions="Work."),
        model=FakeModel([ModelResponse(content="one")]),
    )
    second = Agent(
        profile=AgentProfile(id="second", instructions="Work."),
        model=FakeModel([ModelResponse(content="two")]),
    )

    first_handle, second_handle = await asyncio.gather(
        first.start("First"),
        second.start("Second"),
    )
    first_result, second_result = await asyncio.gather(
        first_handle.result(),
        second_handle.result(),
    )

    assert first_handle.run_id != second_handle.run_id
    assert first_result.output == "one"
    assert second_result.output == "two"
    assert {event.run_id for event in await first_handle.events()} == {first_handle.run_id}
    assert {event.run_id for event in await second_handle.events()} == {second_handle.run_id}


@pytest.mark.asyncio
async def test_start_propagates_validation_failure_without_leaking_a_task() -> None:
    agent = Agent(
        profile=AgentProfile(id="invalid", instructions="Work."),
        model=FakeModel([]),
    )

    with pytest.raises(ValueError, match="prompt"):
        await agent.start(" ")
