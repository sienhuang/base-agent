import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest

from base_agent import Agent, AgentDefinition
from base_agent.events import EventStore, EventStream, InMemoryEventStore
from base_agent.models import Event
from base_agent.run_handle import EventStreamingNotSupportedError
from base_agent.testing import FakeModel


async def _collect_events(stream: AsyncIterator[Event]) -> tuple[Event, ...]:
    return tuple([event async for event in stream])


async def test_in_memory_store_assigns_per_run_sequence_and_replays_by_cursor() -> None:
    store = InMemoryEventStore()

    first = await store.emit("run-a", "run.started")
    second = await store.emit("run-a", "model.requested", {"turn": 1})
    other = await store.emit("run-b", "run.started")

    assert isinstance(store, EventStore)
    assert isinstance(store, EventStream)
    assert (first.sequence, second.sequence, other.sequence) == (1, 2, 1)
    assert await store.list("run-a") == (first, second)
    assert await store.list("run-a", after_sequence=1) == (second,)
    assert await store.list("missing") == ()


async def test_in_memory_store_assigns_unique_sequences_under_concurrent_emits() -> None:
    store = InMemoryEventStore()

    emitted = await asyncio.gather(
        *(store.emit("run-a", f"event.{index}") for index in range(100))
    )
    persisted = await store.list("run-a")

    assert sorted(event.sequence for event in emitted) == list(range(1, 101))
    assert [event.sequence for event in persisted] == list(range(1, 101))
    assert {event.kind for event in persisted} == {
        f"event.{index}" for index in range(100)
    }


async def test_in_memory_stream_replays_then_follows_committed_events() -> None:
    store = InMemoryEventStore()
    await store.emit("run-a", "run.started")
    stream = store.subscribe("run-a")

    assert (await anext(stream)).kind == "run.started"
    waiting = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    assert waiting.done() is False

    emitted = await store.emit("run-a", "run.succeeded")

    assert await waiting is emitted
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


async def test_completed_stream_stops_at_and_beyond_terminal_cursor() -> None:
    store = InMemoryEventStore()
    await store.emit("run-a", "run.started")
    terminal = await store.emit("run-a", "run.succeeded")

    at_terminal = await asyncio.wait_for(
        _collect_events(store.subscribe("run-a", after_sequence=terminal.sequence)),
        timeout=1,
    )
    beyond_terminal = await asyncio.wait_for(
        _collect_events(store.subscribe("run-a", after_sequence=terminal.sequence + 100)),
        timeout=1,
    )

    assert at_terminal == ()
    assert beyond_terminal == ()


async def test_harness_store_is_the_single_event_record_and_observers_are_fanout() -> None:
    class RecordingEventStore(InMemoryEventStore):
        def __init__(self) -> None:
            super().__init__()
            self.emitted: list[Event] = []

        async def emit(
            self,
            run_id: str,
            kind: str,
            payload: Mapping[str, Any] | None = None,
        ) -> Event:
            event = await super().emit(run_id, kind, payload)
            self.emitted.append(event)
            return event

    store = RecordingEventStore()
    observed: list[Event] = []
    agent = Agent(
        AgentDefinition(
            id="stored-events",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        event_store=store,
        observers=(observed.append,),
    )

    result = await agent.run("hello")
    persisted = await store.list(result.run_id)

    assert result.events == persisted == tuple(store.emitted) == tuple(observed)
    assert all(
        result_event is stored_event
        for result_event, stored_event in zip(result.events, persisted, strict=True)
    )


async def test_event_is_committed_before_observer_notification() -> None:
    store = InMemoryEventStore()
    observed_after_commit: list[bool] = []

    async def observe(event: Event) -> None:
        persisted = await store.list(event.run_id)
        observed_after_commit.append(bool(persisted) and persisted[-1] == event)

    agent = Agent(
        AgentDefinition(
            id="commit-before-observe",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        event_store=store,
        observers=(observe,),
    )

    result = await agent.run("hello")

    assert len(observed_after_commit) == len(result.events)
    assert all(observed_after_commit)


async def test_event_store_failure_does_not_notify_observers_or_call_model() -> None:
    class FailingEventStore:
        async def emit(
            self,
            run_id: str,
            kind: str,
            payload: Mapping[str, Any] | None = None,
        ) -> Event:
            raise RuntimeError("event store unavailable")

        async def list(
            self,
            run_id: str,
            *,
            after_sequence: int = 0,
        ) -> tuple[Event, ...]:
            return ()

    model = FakeModel.text("unused")
    observed: list[Event] = []
    agent = Agent(
        AgentDefinition(
            id="failed-event-store",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
        event_store=FailingEventStore(),
        observers=(observed.append,),
    )

    with pytest.raises(RuntimeError, match="event store unavailable"):
        await agent.run("hello")

    assert observed == []
    assert model.requests == ()


async def test_run_handle_reports_history_only_store_cannot_stream() -> None:
    class HistoryOnlyEventStore:
        def __init__(self) -> None:
            self.delegate = InMemoryEventStore()

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

    store = HistoryOnlyEventStore()
    agent = Agent(
        AgentDefinition(
            id="history-only-events",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        event_store=store,
    )
    handle = agent.start("hello")

    with pytest.raises(EventStreamingNotSupportedError, match="does not support live"):
        handle.stream()

    result = await handle.wait()
    assert await handle.events() == result.events


async def test_event_store_rejects_negative_cursor() -> None:
    store = InMemoryEventStore()

    with pytest.raises(ValueError, match="after_sequence must be non-negative"):
        await store.list("run-a", after_sequence=-1)
    with pytest.raises(ValueError, match="after_sequence must be non-negative"):
        await anext(store.subscribe("run-a", after_sequence=-1))
