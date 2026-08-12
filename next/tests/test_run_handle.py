import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from typing import Any

import pytest

from base_agent import Agent, AgentDefinition, RunHandle, RunStatus, tool
from base_agent.events import InMemoryEventStore
from base_agent.memory import MemoryObservation, MemoryScope
from base_agent.models import (
    Event,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ToolCall,
)
from base_agent.providers import ProviderCapabilities, ProviderInfo
from base_agent.run_handle import RunNotCancellableError
from base_agent.testing import FakeModel


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
        "context.prepared",
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


async def test_handle_cancel_stops_background_run_and_persists_terminal_event() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)

    result = await handle.cancel()

    assert result.status is RunStatus.CANCELLED
    assert result.error == "run cancellation requested"
    assert result.output is None
    assert result.text == ""
    assert handle.status is RunStatus.CANCELLED
    assert await handle.wait() is result
    assert [event.kind for event in result.events] == [
        "run.started",
        "message.created",
        "run.ready",
        "context.prepared",
        "model.requested",
        "run.cancelled",
    ]
    terminal = result.events[-1]
    assert terminal.turn == 1
    assert terminal.payload == {"reason": "run cancellation requested"}


async def test_handle_cancel_does_not_queue_memory_write() -> None:
    class RecordingMemoryWriter:
        def __init__(self) -> None:
            self.observations: list[MemoryObservation] = []

        @property
        def name(self) -> str:
            return "recording-writer"

        @property
        def enabled(self) -> bool:
            return True

        def enqueue(self, observation: MemoryObservation, /) -> None:
            self.observations.append(observation)

    writer = RecordingMemoryWriter()
    model = GatedModel()
    agent = Agent(
        AgentDefinition(
            id="cancelled-memory-write",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
        memory_writer=writer,
    )
    handle = agent.start("hello", memory_scope=MemoryScope(("user-1",)))
    await asyncio.wait_for(model.requested.wait(), timeout=1)

    result = await handle.cancel()

    assert result.status is RunStatus.CANCELLED
    assert writer.observations == []
    assert not any(event.kind.startswith("memory.write.") for event in result.events)


async def test_handle_can_be_cancelled_immediately_after_start() -> None:
    handle = _agent(GatedModel()).start("hello")

    result = await handle.cancel()

    assert result.status is RunStatus.CANCELLED
    assert result.events[0].kind == "run.started"
    assert result.events[-1].kind == "run.cancelled"


async def test_handle_cancel_closes_an_in_progress_tool_event() -> None:
    tool_started = asyncio.Event()
    tool_release = asyncio.Event()

    @tool
    async def blocking_tool() -> str:
        tool_started.set()
        await tool_release.wait()
        return "done"

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="blocking-1",
                        name="blocking_tool",
                        arguments={},
                    ),
                )
            ),
            ModelResponse(content="unused"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="tool-cancellation",
            version="1.0.0",
            instructions="Call the Tool.",
            tools=("blocking_tool",),
            max_model_turns=2,
        ),
        model=model,
        tools=(blocking_tool,),
    )
    handle = agent.start("work")
    await asyncio.wait_for(tool_started.wait(), timeout=1)

    result = await handle.cancel()

    tool_events = [event for event in result.events if event.kind.startswith("tool.")]
    assert [event.kind for event in tool_events] == ["tool.started", "tool.completed"]
    assert tool_events[-1].turn == 1
    assert tool_events[-1].payload["status"] == "cancelled"
    assert result.events[-1].kind == "run.cancelled"


async def test_handle_cancel_closes_tool_started_before_observer_returns() -> None:
    tool_started_committed = asyncio.Event()
    observer_release = asyncio.Event()

    async def blocking_observer(event: Event) -> None:
        if event.kind == "tool.started":
            tool_started_committed.set()
            await observer_release.wait()

    @tool
    async def never_invoked() -> str:
        raise AssertionError("cancellation should happen before Tool invocation")

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="observer-blocked-1",
                        name="never_invoked",
                        arguments={},
                    ),
                )
            ),
            ModelResponse(content="unused"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="tool-start-observer-cancellation",
            version="1.0.0",
            instructions="Call the Tool.",
            tools=("never_invoked",),
            max_model_turns=2,
        ),
        model=model,
        tools=(never_invoked,),
        observers=(blocking_observer,),
    )
    handle = agent.start("work")
    await asyncio.wait_for(tool_started_committed.wait(), timeout=1)

    result = await handle.cancel()

    tool_events = [event for event in result.events if event.kind.startswith("tool.")]
    assert [event.kind for event in tool_events] == ["tool.started", "tool.completed"]
    assert tool_events[-1].payload["status"] == "cancelled"
    assert result.events[-1].kind == "run.cancelled"


async def test_handle_cancel_is_idempotent_only_for_cancelled_run() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)

    first, second = await asyncio.gather(handle.cancel(), handle.cancel())
    repeated = await handle.cancel()

    assert first is second is repeated
    assert [event.kind for event in first.events].count("run.cancelled") == 1

    completed_model = GatedModel()
    completed_handle = _agent(completed_model).start("hello")
    await asyncio.wait_for(completed_model.requested.wait(), timeout=1)
    completed_model.release.set()
    await completed_handle.wait()

    with pytest.raises(RunNotCancellableError, match="already reached status 'succeeded'"):
        await completed_handle.cancel()


async def test_cancel_does_not_rewrite_a_committed_terminal_event() -> None:
    terminal_committed = asyncio.Event()
    release_observer = asyncio.Event()

    async def blocking_observer(event: Event) -> None:
        if event.kind == "run.succeeded":
            terminal_committed.set()
            await release_observer.wait()

    agent = Agent(
        AgentDefinition(
            id="terminal-cancel-race",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        observers=(blocking_observer,),
    )
    handle = agent.start("hello")
    await asyncio.wait_for(terminal_committed.wait(), timeout=1)

    with pytest.raises(RunNotCancellableError, match="already committed status 'succeeded'"):
        await asyncio.wait_for(handle.cancel(), timeout=1)

    release_observer.set()
    result = await handle.wait()
    assert result.status is RunStatus.SUCCEEDED
    assert [event.kind for event in result.events].count("run.succeeded") == 1
    assert "run.cancelled" not in [event.kind for event in result.events]


async def test_direct_agent_run_cancellation_still_propagates_to_caller() -> None:
    model = GatedModel()
    task = asyncio.create_task(_agent(model).run("hello"))
    await asyncio.wait_for(model.requested.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_event_stream_replays_then_follows_until_run_completion() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)
    event_stream = handle.stream(after_sequence=1)

    created = await anext(event_stream)
    assert created.kind == "message.created"
    assert created.payload["content"] == "hello"
    assert (await anext(event_stream)).kind == "run.ready"
    assert (await anext(event_stream)).kind == "context.prepared"
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


async def test_multiple_event_subscribers_receive_run_cancelled() -> None:
    model = GatedModel()
    handle = _agent(model).start("hello")
    await asyncio.wait_for(model.requested.wait(), timeout=1)
    first_subscriber = asyncio.create_task(_collect_events(handle.stream()))
    second_subscriber = asyncio.create_task(_collect_events(handle.stream()))
    await asyncio.sleep(0)

    result = await handle.cancel()

    assert result.status is RunStatus.CANCELLED
    assert await first_subscriber == result.events
    assert await second_subscriber == result.events
    assert result.events[-1].kind == "run.cancelled"


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
            *,
            turn: int | None = None,
        ) -> Event:
            return await self.delegate.emit(run_id, kind, payload, turn=turn)

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
        event.payload["delta"] for event in await handle.events() if event.kind == "message.delta"
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
    assert [event.kind for event in await handle.events(after_sequence=5)] == [
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
