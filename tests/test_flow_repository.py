from uuid import UUID

import pytest

from base_agent import (
    AgentDefinition,
    AgentInvocationInput,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentResultStatus,
    EventType,
    FlowAgent,
    FlowDefinition,
    FlowEventDraft,
    FlowLifecycle,
    FlowRevisionConflictError,
    InMemoryFlowRepository,
    PendingInput,
    RunStatus,
    TokenUsage,
)


def make_flow() -> FlowDefinition:
    return FlowDefinition(
        id="research-report",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="researcher",
                definition=AgentDefinition(
                    id="research-agent",
                    version="1.0.0",
                    instructions="Research.",
                ),
            ),
        ),
        strategy="sequential",
        max_invocations=2,
    )


def request_for(
    run_id: UUID,
    definition: FlowDefinition,
    *,
    prompt: str = "private research request",
) -> AgentInvocationRequest:
    agent_definition = definition.agent("researcher")
    return AgentInvocationRequest(
        flow_run_id=run_id,
        sequence=1,
        agent_key="researcher",
        definition_id=agent_definition.id,
        definition_version=agent_definition.version,
        definition_fingerprint=agent_definition.fingerprint,
        input=AgentInvocationInput(prompt=prompt),
    )


@pytest.mark.asyncio
async def test_lifecycle_persists_revisions_and_ordered_events() -> None:
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    definition = make_flow()

    created = await lifecycle.create(definition)
    started = await lifecycle.start(created.run_id)
    request = request_for(created.run_id, definition)
    invoking = await lifecycle.begin_invocation(
        created.run_id,
        request,
        definition=definition,
    )
    settled = await lifecycle.settle_invocation(
        created.run_id,
        AgentInvocationResult(
            flow_run_id=created.run_id,
            invocation_id=request.invocation_id,
            agent_key=request.agent_key,
            status=AgentResultStatus.COMPLETED,
            output="private result",
            usage=TokenUsage(input_tokens=5, output_tokens=2),
        ),
    )
    completed = await lifecycle.complete(created.run_id, "private final output")

    assert [
        created.revision,
        started.revision,
        invoking.revision,
        settled.revision,
        completed.revision,
    ] == [1, 2, 3, 4, 5]
    assert await repository.get(created.run_id) == completed
    events = await repository.events(created.run_id)
    assert [event.sequence for event in events] == [1, 2, 3, 4, 5]
    assert [event.type for event in events] == [
        EventType.FLOW_CREATED,
        EventType.FLOW_STARTED,
        EventType.AGENT_INVOCATION_STARTED,
        EventType.AGENT_INVOCATION_COMPLETED,
        EventType.FLOW_COMPLETED,
    ]
    serialized_events = " ".join(event.model_dump_json() for event in events)
    assert "private research request" not in serialized_events
    assert "private result" not in serialized_events
    assert "private final output" not in serialized_events


@pytest.mark.asyncio
async def test_waiting_snapshot_and_events_are_committed_together() -> None:
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    definition = make_flow()
    state = await lifecycle.create(definition)
    state = await lifecycle.start(state.run_id)
    request = request_for(state.run_id, definition)
    state = await lifecycle.begin_invocation(
        state.run_id,
        request,
        definition=definition,
    )

    waiting = await lifecycle.settle_invocation(
        state.run_id,
        AgentInvocationResult(
            flow_run_id=state.run_id,
            invocation_id=request.invocation_id,
            agent_key=request.agent_key,
            status=AgentResultStatus.WAITING,
            usage=TokenUsage(input_tokens=4, output_tokens=1),
            pending_input=PendingInput(
                tool_call_id="call-1",
                tool_name="ask_user",
                prompt="private clarification",
            ),
        ),
    )

    persisted = await repository.get(state.run_id)
    events = await repository.events(state.run_id)
    assert persisted == waiting
    assert persisted.status is RunStatus.WAITING
    assert [event.type for event in events[-2:]] == [
        EventType.AGENT_INVOCATION_WAITING,
        EventType.FLOW_WAITING,
    ]
    assert events[-2].sequence + 1 == events[-1].sequence
    assert "private clarification" not in " ".join(
        event.model_dump_json() for event in events
    )


@pytest.mark.asyncio
async def test_terminating_active_flow_emits_child_before_parent_event() -> None:
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    definition = make_flow()
    state = await lifecycle.create(definition)
    state = await lifecycle.start(state.run_id)
    request = request_for(state.run_id, definition)
    state = await lifecycle.begin_invocation(
        state.run_id,
        request,
        definition=definition,
    )

    cancelled = await lifecycle.cancel(state.run_id)

    assert cancelled.status is RunStatus.CANCELLED
    assert cancelled.invocations[0].status is RunStatus.CANCELLED
    events = await repository.events(state.run_id)
    assert [event.type for event in events[-2:]] == [
        EventType.AGENT_INVOCATION_CANCELLED,
        EventType.FLOW_CANCELLED,
    ]


@pytest.mark.asyncio
async def test_repository_rejects_stale_aggregate_commit() -> None:
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    created = await lifecycle.create(make_flow())
    first_reader = await repository.get(created.run_id)
    second_reader = await repository.get(created.run_id)
    first_update = first_reader.start()
    stale_update = second_reader.start()

    await repository.commit(
        first_update,
        expected_revision=first_reader.revision,
        events=(FlowEventDraft(type=EventType.FLOW_STARTED),),
    )

    with pytest.raises(FlowRevisionConflictError, match="revision conflict"):
        await repository.commit(
            stale_update,
            expected_revision=second_reader.revision,
            events=(FlowEventDraft(type=EventType.FLOW_STARTED),),
        )


@pytest.mark.asyncio
async def test_repository_returns_defensive_event_copies() -> None:
    repository = InMemoryFlowRepository()
    lifecycle = FlowLifecycle(repository)
    state = await lifecycle.create(make_flow())

    first_read = await repository.events(state.run_id)
    first_read[0].data["definition_id"] = "mutated"
    second_read = await repository.events(state.run_id)

    assert second_read[0].data["definition_id"] == "research-report"
