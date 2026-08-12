import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Any

import pytest

from base_agent import Agent, AgentDefinition, RunHandle, RunStatus
from base_agent.events import InMemoryEventStore
from base_agent.models import (
    Event,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
)
from base_agent.providers import ProviderCapabilities, ProviderInfo


class GatedModel:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="gated-model",
            default_model="gated-model",
            capabilities=ProviderCapabilities(),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requested.set()
        await self.release.wait()
        return ModelResponse(content="done")


def _agent(model: GatedModel) -> Agent:
    return Agent(
        AgentDefinition(
            id="background-assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
    )


async def _collect_events(stream: AsyncIterator[Event]) -> tuple[Event, ...]:
    events = []
    async for event in stream:
        events.append(event)
    return tuple(events)


async def test_start_returns_running_handle_before_model_completes() -> None:
    model = GatedModel()

    handle = _agent(model).start("hello")

    assert isinstance(handle, RunHandle)
    assert handle.run_id
    assert handle.status is RunStatus.RUNNING
    await asyncio.wait_for(model.requested.wait(), timeout=1)
    running_events = await handle.events()
    assert [event.kind for event in running_events] == [
        "run.started",
        "message.created",
        "run.ready",
        "model.requested",
    ]
    assert {event.run_id for event in running_events} == {handle.run_id}

    cursor = running_events[-1].sequence
    assert await handle.events(after_sequence=cursor) == ()
    model.release.set()
    result = await handle.wait()

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "done"
    assert result.run_id == handle.run_id
    assert handle.status is RunStatus.SUCCEEDED
    assert await handle.events() == result.events
    assert [event.kind for event in await handle.events(after_sequence=cursor)] == [
        "model.responded",
        "message.completed",
        "run.succeeded",
    ]


async def test_cancelling_waiter_does_not_cancel_background_run() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)
    waiter = asyncio.create_task(handle.wait())

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert handle.status is RunStatus.RUNNING
    model.release.set()
    result = await handle.wait()
    assert result.status is RunStatus.SUCCEEDED


async def test_event_stream_replays_then_follows_until_run_completion() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)
    event_stream = handle.stream(after_sequence=1)

    created = await anext(event_stream)
    assert created.kind == "message.created"
    assert created.payload["content"] == "hello"
    assert (await anext(event_stream)).kind == "run.ready"
    assert (await anext(event_stream)).kind == "model.requested"
    next_event = asyncio.create_task(anext(event_stream))
    await asyncio.sleep(0)
    assert next_event.done() is False

    model.release.set()
    assert (await next_event).kind == "model.responded"
    completed = await anext(event_stream)
    assert completed.kind == "message.completed"
    assert completed.payload["content"] == "done"
    assert (await anext(event_stream)).kind == "run.succeeded"
    with pytest.raises(StopAsyncIteration):
        await anext(event_stream)


async def test_event_stream_replays_completed_run_and_supports_multiple_subscribers() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)

    first_subscriber = asyncio.create_task(_collect_events(handle.stream()))
    second_subscriber = asyncio.create_task(_collect_events(handle.stream()))
    await asyncio.sleep(0)
    model.release.set()
    result = await handle.wait()

    assert await first_subscriber == result.events
    assert await second_subscriber == result.events
    assert await _collect_events(handle.stream(after_sequence=3)) == result.events[3:]


async def test_cancelling_event_subscription_does_not_cancel_background_run() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)
    subscription = handle.stream(after_sequence=3)
    waiting_for_event = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    waiting_for_event.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_for_event

    assert handle.status is RunStatus.RUNNING
    model.release.set()
    assert (await handle.wait()).status is RunStatus.SUCCEEDED


async def test_closing_handle_stream_closes_underlying_subscription() -> None:
    class TrackingEventStore:
        def __init__(self) -> None:
            self.delegate = InMemoryEventStore()
            self.subscription_closed = asyncio.Event()

        async def emit(
            self,
            run_id: str,
            kind: str,
            payload: Mapping[str, Any] | None = None,
        ) -> Event:
            return await self.delegate.emit(run_id, kind, payload)

        async def list(
            self,
            run_id: str,
            *,
            after_sequence: int = 0,
        ) -> tuple[Event, ...]:
            return await self.delegate.list(run_id, after_sequence=after_sequence)

        async def subscribe(
            self,
            run_id: str,
            *,
            after_sequence: int = 0,
        ) -> AsyncGenerator[Event, None]:
            try:
                async for event in self.delegate.subscribe(
                    run_id,
                    after_sequence=after_sequence,
                ):
                    yield event
            finally:
                self.subscription_closed.set()

    store = TrackingEventStore()
    model = GatedModel()
    agent = Agent(
        AgentDefinition(
            id="subscription-cleanup",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
        event_store=store,
    )
    handle = agent.start("hello")
    event_stream = handle.stream()

    assert (await anext(event_stream)).kind == "run.started"
    await event_stream.aclose()

    await asyncio.wait_for(store.subscription_closed.wait(), timeout=1)
    assert handle.status is RunStatus.RUNNING
    model.release.set()
    await handle.wait()


async def test_event_stream_closes_when_run_raises_before_emitting_events() -> None:
    handle = _agent(GatedModel()).start(" ")

    events = await asyncio.wait_for(_collect_events(handle.stream()), timeout=1)

    assert events == ()
    with pytest.raises(ValueError, match="run prompt must not be blank"):
        await handle.wait()
    assert handle.status is RunStatus.FAILED


async def test_start_can_preserve_provider_streaming_events() -> None:
    class StreamingModel:
        @property
        def info(self) -> ProviderInfo:
            return ProviderInfo(
                name="streaming-model",
                default_model="streaming-model",
                capabilities=ProviderCapabilities(streaming=True),
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming background Run must not call complete()")

        async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
            yield ModelTextDelta("do")
            yield ModelTextDelta("ne")
            yield ModelStreamCompleted(ModelResponse(content="done"))

    agent = Agent(
        AgentDefinition(
            id="streaming-background-assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=StreamingModel(),
    )

    handle = agent.start("hello", stream=True)
    result = await handle.wait()

    assert result.text == "done"
    assert [
        event.payload["delta"]
        for event in await handle.events()
        if event.kind == "message.delta"
    ] == ["do", "ne"]


async def test_handle_reports_failed_terminal_status() -> None:
    class FailingModel(GatedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("model unavailable")

    handle = _agent(FailingModel()).start("hello")

    result = await handle.wait()

    assert result.status is RunStatus.FAILED
    assert handle.status is RunStatus.FAILED
    assert await handle.events() == result.events
    assert [event.kind for event in await handle.events(after_sequence=4)] == [
        "model.failed",
        "run.failed",
    ]


async def test_events_rejects_negative_cursor() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")

    with pytest.raises(ValueError, match="after_sequence must be non-negative"):
        await handle.events(after_sequence=-1)
    with pytest.raises(ValueError, match="after_sequence must be non-negative"):
        handle.stream(after_sequence=-1)

    model.release.set()
    await handle.wait()
