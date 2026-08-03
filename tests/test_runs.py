import asyncio
from uuid import UUID, uuid4

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventStore,
    EventType,
    InMemoryEventStore,
    InMemoryRunStore,
    ModelRequest,
    ModelResponse,
    Run,
    RunStatus,
    RunStore,
    ToolCall,
    tool,
)
from base_agent.stores import (
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunNotFoundError,
)
from base_agent.testing import FakeModel


@pytest.mark.asyncio
async def test_completed_run_is_queryable_and_events_are_replayable() -> None:
    agent = Agent(
        profile=AgentProfile(id="assistant", instructions="Be helpful."),
        model=FakeModel([ModelResponse(content="done")]),
    )

    result = await agent.run("work")
    run_id = result.metadata["run_id"]
    stored = await agent.get_run(_uuid(run_id))
    events = await agent.events(stored.id)

    assert stored.status is RunStatus.COMPLETED
    assert stored.output == "done"
    assert stored.step_count == 1
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert [event.type for event in events] == [
        EventType.RUN_CREATED,
        EventType.RUN_STARTED,
        EventType.MODEL_REQUESTED,
        EventType.MODEL_RESPONDED,
        EventType.RUN_COMPLETED,
    ]
    assert events[-1].data["output"] == "done"


@pytest.mark.asyncio
async def test_tool_lifecycle_is_visible_in_event_history() -> None:
    @tool
    async def echo(value: str) -> str:
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="call-1", name="echo", arguments={"value": "x"}),)
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("echo",),
        ),
        model=model,
        tools=[echo],
    )

    result = await agent.run("echo")
    events = await agent.events(_uuid(result.metadata["run_id"]))

    event_types = [event.type for event in events]
    assert event_types.count(EventType.MODEL_REQUESTED) == 2
    assert event_types.count(EventType.MODEL_RESPONDED) == 2
    assert EventType.TOOL_REQUESTED in event_types
    assert EventType.TOOL_STARTED in event_types
    assert EventType.TOOL_COMPLETED in event_types
    assert EventType.TOOL_FAILED not in event_types
    assert event_types[-1] is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_cancellation_does_not_start_the_next_tool_or_model_step() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    execution_order: list[str] = []

    @tool
    async def controlled(value: str) -> str:
        execution_order.append(value)
        if value == "first":
            first_started.set()
            await release_first.wait()
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-1", name="controlled", arguments={"value": "first"}),
                    ToolCall(id="call-2", name="controlled", arguments={"value": "second"}),
                )
            ),
            ModelResponse(content="must not be consumed"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("controlled",),
        ),
        model=model,
        tools=[controlled],
    )
    run_id = uuid4()

    task = asyncio.create_task(agent.run("start", run_id=run_id))
    await asyncio.wait_for(first_started.wait(), timeout=1)
    requested = await agent.cancel(run_id)
    release_first.set()
    result = await asyncio.wait_for(task, timeout=1)

    stored = await agent.get_run(run_id)
    events = await agent.events(run_id)
    assert requested.cancel_requested is True
    assert result.status is AgentResultStatus.CANCELLED
    assert stored.status is RunStatus.CANCELLED
    assert stored.cancel_requested is True
    assert execution_order == ["first"]
    assert len(model.requests) == 1
    assert [event.type for event in events].count(EventType.TOOL_REQUESTED) == 1
    assert events[-1].type is EventType.RUN_CANCELLED


@pytest.mark.asyncio
async def test_task_cancellation_persists_an_unrecoverable_interruption() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel:
        name = "interruptible"

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            started.set()
            await release.wait()
            return ModelResponse(content="too late")

    agent = Agent(
        profile=AgentProfile(id="interrupted", instructions="Work."),
        model=BlockingModel(),
    )
    run_id = uuid4()
    task = asyncio.create_task(agent.run("work", run_id=run_id))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await agent.get_run(run_id)
    events = await agent.events(run_id)
    assert stored.status is RunStatus.INTERRUPTED
    assert stored.error == "runtime task interrupted"
    assert stored.cancel_requested is False
    assert stored.metadata["interruption"] == {
        "source": "asyncio_task_cancelled",
        "recoverable": False,
    }
    assert [event.type for event in events].count(EventType.RUN_INTERRUPTED) == 1
    assert events[-1].type is EventType.RUN_INTERRUPTED
    assert events[-1].data["interruption_source"] == "asyncio_task_cancelled"
    assert events[-1].data["recoverable"] is False


@pytest.mark.asyncio
async def test_accepted_business_cancellation_wins_over_task_interruption() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel:
        name = "cancel-then-interrupt"

        async def complete(self, request: ModelRequest) -> ModelResponse:
            del request
            started.set()
            await release.wait()
            return ModelResponse(content="too late")

    agent = Agent(
        profile=AgentProfile(id="cancel-then-interrupt", instructions="Work."),
        model=BlockingModel(),
    )
    run_id = uuid4()
    task = asyncio.create_task(agent.run("work", run_id=run_id))
    await asyncio.wait_for(started.wait(), timeout=1)

    requested = await agent.cancel(run_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await agent.get_run(run_id)
    events = await agent.events(run_id)
    assert requested.cancel_requested is True
    assert stored.status is RunStatus.CANCELLED
    assert stored.cancel_requested is True
    assert events[-1].type is EventType.RUN_CANCELLED
    assert EventType.RUN_INTERRUPTED not in {event.type for event in events}


@pytest.mark.asyncio
async def test_stale_runtime_snapshot_cannot_clear_cancellation_request() -> None:
    class CancellationRaceRunStore(InMemoryRunStore):
        def __init__(self) -> None:
            super().__init__()
            self.arm_snapshot_race = False
            self.snapshot_read = asyncio.Event()
            self.release_snapshot = asyncio.Event()
            self._intercepted = False

        async def get(self, run_id: UUID) -> Run:
            run = await super().get(run_id)
            if self.arm_snapshot_race and not self._intercepted:
                self._intercepted = True
                self.snapshot_read.set()
                await self.release_snapshot.wait()
            return run

    store = CancellationRaceRunStore()

    class ArmRaceModel(FakeModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            store.arm_snapshot_race = True
            return response

    agent = Agent(
        profile=AgentProfile(id="cancel-race", instructions="Work."),
        model=ArmRaceModel([ModelResponse(content="too late")]),
        run_store=store,
    )
    run_id = uuid4()

    task = asyncio.create_task(agent.run("work", run_id=run_id))
    await asyncio.wait_for(store.snapshot_read.wait(), timeout=1)
    requested = await agent.cancel(run_id)
    store.release_snapshot.set()
    result = await asyncio.wait_for(task, timeout=1)

    stored = await agent.get_run(run_id)
    assert requested.cancel_requested is True
    assert result.status is AgentResultStatus.CANCELLED
    assert stored.status is RunStatus.CANCELLED
    assert stored.cancel_requested is True
    assert (await agent.events(run_id))[-1].type is EventType.RUN_CANCELLED


@pytest.mark.asyncio
async def test_in_memory_stores_enforce_identity_and_copy_boundaries() -> None:
    run_store = InMemoryRunStore()
    event_store = InMemoryEventStore()
    run = Run(profile_id="assistant")

    assert isinstance(run_store, RunStore)
    assert isinstance(event_store, EventStore)

    await run_store.create(run)
    with pytest.raises(RunAlreadyExistsError):
        await run_store.create(run)
    with pytest.raises(RunNotFoundError):
        await run_store.get(uuid4())

    event = await event_store.emit(run.id, EventType.RUN_CREATED, {"nested": {"value": 1}})
    event.data["nested"]["value"] = 999
    replayed = await event_store.list(run.id)

    assert replayed[0].data["nested"]["value"] == 1
    assert replayed[0].sequence == 1


@pytest.mark.asyncio
async def test_interrupted_event_is_a_permanent_stream_boundary() -> None:
    event_store = InMemoryEventStore()
    run_id = uuid4()
    await event_store.emit(run_id, EventType.RUN_CREATED)
    await event_store.emit(run_id, EventType.RUN_INTERRUPTED)

    replayed = [
        event.type
        async for event in event_store.subscribe(run_id, after_sequence=0)
    ]

    assert replayed == [EventType.RUN_CREATED, EventType.RUN_INTERRUPTED]


@pytest.mark.asyncio
async def test_terminal_run_cannot_be_cancelled_after_completion() -> None:
    agent = Agent(
        profile=AgentProfile(id="assistant", instructions="Be helpful."),
        model=FakeModel([ModelResponse(content="done")]),
    )
    result = await agent.run("work")
    run_id = _uuid(result.metadata["run_id"])

    with pytest.raises(RunNotCancellableError, match="completed.*cannot be cancelled"):
        await agent.cancel(run_id)

    assert (await agent.get_run(run_id)).cancel_requested is False


@pytest.mark.asyncio
async def test_waiting_run_remains_cancellable() -> None:
    store = InMemoryRunStore()
    waiting = Run(profile_id="remote", status=RunStatus.WAITING)
    await store.create(waiting)

    requested = await store.request_cancel(waiting.id)

    assert requested.status is RunStatus.WAITING
    assert requested.cancel_requested is True


def _uuid(value: object) -> UUID:
    return UUID(str(value))
