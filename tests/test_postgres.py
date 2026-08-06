import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    AgentProfile,
    AgentResultStatus,
    Conversation,
    ConversationStore,
    EventType,
    FlowAgent,
    FlowDefinition,
    FlowEventDraft,
    FlowLeaseLostError,
    FlowLifecycle,
    FlowRevisionConflictError,
    FlowSideEffect,
    FlowSideEffectPhase,
    FlowSideEffectRetryMode,
    FlowWorkCommand,
    FlowWorkReview,
    FlowWorkReviewDecision,
    FlowWorkStatus,
    ModelResponse,
    Run,
    RunStatus,
    RuntimeCheckpoint,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.stores import (
    ArtifactStore,
    CheckpointNotFoundError,
    CheckpointStore,
    ConversationBusyError,
    EventStore,
    EventStream,
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunStore,
)
from base_agent.stores.postgres import (
    PostgresFlowRepository,
    PostgresFlowSideEffectLedger,
    PostgresFlowWorkSource,
    PostgresStore,
)
from base_agent.stores.postgres.schema import build_tables
from base_agent.testing import FakeModel

POSTGRES_DSN = os.getenv("BASE_AGENT_TEST_POSTGRES_DSN")
requires_postgres = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set BASE_AGENT_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)


@tool
async def request_region(question: str) -> WaitForInput:
    """Request a region before continuing."""
    return WaitForInput(prompt=question)


def test_schema_name_is_validated_before_building_metadata() -> None:
    tables = build_tables("agent_data")
    assert tables.metadata.schema == "agent_data"
    assert tables.flow_runs.name == "base_agent_flow_runs"
    assert tables.flow_events.name == "base_agent_flow_events"
    assert tables.flow_leases.name == "base_agent_flow_leases"
    assert tables.flow_work_items.name == "base_agent_flow_work_items"
    assert tables.flow_work_reviews.name == "base_agent_flow_work_reviews"
    assert tables.flow_side_effects.name == "base_agent_flow_side_effects"
    assert {column.name for column in tables.flow_runs.columns} == {
        "id",
        "revision",
        "status",
        "updated_at",
        "payload",
    }
    assert {column.name for column in tables.flow_events.primary_key} == {
        "run_id",
        "sequence",
    }
    assert {column.name for column in tables.flow_leases.columns} == {
        "run_id",
        "token",
        "owner_id",
        "attempt",
        "active",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
    }
    assert {
        "run_id",
        "idempotency_key",
        "status",
        "attempt",
        "delivery_token",
        "lease_expires_at",
        "blocked_reason",
        "payload",
    } <= {column.name for column in tables.flow_work_items.columns}
    assert {
        "work_id",
        "decision",
        "reviewer_id",
        "reason_code",
        "idempotency_key",
        "payload",
    } <= {column.name for column in tables.flow_work_reviews.columns}
    assert {
        "run_id",
        "invocation_id",
        "operation_key",
        "operation_name",
        "retry_mode",
        "phase",
        "revision",
        "payload",
    } <= {column.name for column in tables.flow_side_effects.columns}
    with pytest.raises(ValueError, match="invalid PostgreSQL schema"):
        build_tables("agent-data;drop schema public")


async def open_store() -> AsyncIterator[PostgresStore]:
    assert POSTGRES_DSN is not None
    store = PostgresStore.from_url(POSTGRES_DSN, poll_interval=0.01)
    await store.create_schema()
    try:
        yield store
    finally:
        await store.close()


def postgres_flow_definition() -> FlowDefinition:
    return FlowDefinition(
        id="postgres-flow",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="writer",
                definition=AgentDefinition(
                    id="postgres-writer",
                    version="1.0.0",
                    instructions="Write the result.",
                ),
            ),
        ),
        strategy="sequential",
    )


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_flow_repository_is_durable_and_rejects_stale_writes() -> None:
    async for store in open_store():
        repository = PostgresFlowRepository(store.engine)
        lifecycle = FlowLifecycle(repository)
        created = await lifecycle.create(postgres_flow_definition())
        stale = await repository.get(created.run_id)
        lease = await repository.acquire_execution(
            created.run_id,
            owner_id="postgres-worker",
            ttl_seconds=30,
        )

        with pytest.raises(FlowLeaseLostError):
            await lifecycle.start(created.run_id)
        started = await FlowLifecycle(
            repository,
            execution_lease=lease,
        ).start(created.run_id)

        reopened = PostgresFlowRepository(store.engine)
        assert await reopened.get(created.run_id) == started
        assert [event.type for event in await reopened.events(created.run_id)] == [
            EventType.FLOW_CREATED,
            EventType.FLOW_STARTED,
        ]
        with pytest.raises(FlowRevisionConflictError, match="revision conflict"):
            await reopened.commit(
                stale.start(),
                expected_revision=stale.revision,
                events=(FlowEventDraft(type=EventType.FLOW_STARTED),),
                execution_token=lease.token,
            )


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_flow_work_source_deduplicates_and_fences_delivery() -> None:
    async for store in open_store():
        repository = PostgresFlowRepository(store.engine)
        created = await FlowLifecycle(repository).create(postgres_flow_definition())
        source = PostgresFlowWorkSource(store.engine)
        idempotency_key = f"postgres-work:{uuid4()}"

        first, duplicate = await asyncio.gather(
            source.enqueue(
                FlowWorkCommand(
                    run_id=created.run_id,
                    idempotency_key=idempotency_key,
                    data={"request_id": "postgres-request"},
                )
            ),
            source.enqueue(
                FlowWorkCommand(
                    run_id=created.run_id,
                    idempotency_key=idempotency_key,
                    data={"request_id": "postgres-request"},
                )
            ),
        )
        assert first == duplicate

        claimed = await source.claim(
            owner_id="postgres-worker",
            ttl_seconds=30,
        )
        assert claimed is not None
        assert claimed.id == first.id
        assert claimed.delivery_token is not None
        completed = await source.complete(
            claimed.id,
            delivery_token=claimed.delivery_token,
        )
        repeated = await source.complete(
            claimed.id,
            delivery_token=claimed.delivery_token,
        )
        assert completed == repeated
        assert completed.status is FlowWorkStatus.COMPLETED

        review_command = FlowWorkCommand(
            run_id=created.run_id,
            idempotency_key=f"postgres-review-work:{uuid4()}",
        )
        review_item = await source.enqueue(review_command)
        review_claim = await source.claim(
            owner_id="postgres-review-worker",
            ttl_seconds=30,
        )
        assert review_claim is not None
        assert review_claim.id == review_item.id
        assert review_claim.delivery_token is not None
        blocked = await source.block(
            review_claim.id,
            delivery_token=review_claim.delivery_token,
            reason_code="active_invocation_uncertain",
        )
        assert blocked in await source.list_blocked()
        review = FlowWorkReview(
            work_id=blocked.id,
            decision=FlowWorkReviewDecision.APPROVE_RETRY,
            reviewer_id="postgres-operator",
            reason_code="idempotency_verified",
            idempotency_key=f"postgres-review:{uuid4()}",
        )
        reviewed = await source.review(review)
        repeated_review = await source.review(review)
        assert reviewed == repeated_review
        assert reviewed.item.status is FlowWorkStatus.PENDING
        assert await source.list_reviews(blocked.id) == (reviewed.review,)


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_flow_side_effect_ledger_is_durable_and_fenced() -> None:
    async for store in open_store():
        repository = PostgresFlowRepository(store.engine)
        created = await FlowLifecycle(repository).create(postgres_flow_definition())
        ledger = PostgresFlowSideEffectLedger(store.engine)
        invocation_id = uuid4()
        effect = FlowSideEffect(
            flow_run_id=created.run_id,
            invocation_id=invocation_id,
            operation_key="postgres-tool-call",
            operation_name="payments.charge",
            retry_mode=FlowSideEffectRetryMode.IDEMPOTENT,
            idempotency_key_digest="a" * 64,
        )

        prepared, duplicate = await asyncio.gather(
            ledger.prepare(effect),
            ledger.prepare(effect.model_copy(update={"id": uuid4()})),
        )
        assert prepared == duplicate
        started = await ledger.mark_started(
            prepared.id,
            expected_revision=prepared.revision,
        )
        reopened = PostgresFlowSideEffectLedger(store.engine)
        assert await reopened.list_for_invocation(invocation_id) == (started,)
        confirmed = await reopened.confirm(
            started.id,
            expected_revision=started.revision,
        )
        assert confirmed.phase is FlowSideEffectPhase.CONFIRMED


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_store_implements_core_ports_and_round_trips_data() -> None:
    async for store in open_store():
        assert isinstance(store, RunStore)
        assert isinstance(store, EventStore)
        assert isinstance(store, EventStream)
        assert isinstance(store, CheckpointStore)
        assert isinstance(store, ArtifactStore)

        run = Run(profile_id="postgres-test")
        await store.create(run)
        assert await store.get(run.id) == run
        with pytest.raises(RunAlreadyExistsError):
            await store.create(run)

        events = await asyncio.gather(
            *(store.emit(run.id, EventType.MODEL_REQUESTED, {"index": index}) for index in range(8))
        )
        assert sorted(event.sequence for event in events) == list(range(1, 9))
        assert [event.sequence for event in await store.list(run.id)] == list(range(1, 9))

        attachment_content = b"region,metric\nAPAC,42\n"
        attachment = await store.add_attachment(
            name="input.csv",
            media_type="text/csv",
            content=attachment_content,
            metadata={"source": "integration-test"},
        )
        assert await store.get_attachment(attachment.id) == attachment
        assert await store.read(attachment.id) == attachment_content

        artifact_content = b'{"status":"ok"}'
        artifact = await store.create_artifact(
            run.id,
            name="result.json",
            media_type="application/json",
            content=artifact_content,
        )
        assert await store.get_artifact(artifact.id) == artifact
        assert await store.read(artifact.id) == artifact_content
        assert await store.list_artifacts(run.id) == (artifact,)

        cancelled = await store.request_cancel(run.id)
        assert cancelled.cancel_requested is True
        assert await store.is_cancel_requested(run.id) is True
        stale_snapshot = run.model_copy(
            update={"status": RunStatus.RUNNING},
            deep=True,
        )
        await store.save(stale_snapshot)
        assert (await store.get(run.id)).cancel_requested is True
        await store.save(cancelled.model_copy(update={"status": RunStatus.COMPLETED}))
        with pytest.raises(RunNotCancellableError):
            await store.request_cancel(run.id)


@requires_postgres
@pytest.mark.asyncio
async def test_agent_wait_resume_checkpoint_and_event_stream_are_durable() -> None:
    async for store in open_store():
        model = FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="region-call",
                            name="request_region",
                            arguments={"question": "Which region?"},
                        ),
                    )
                ),
                ModelResponse(content="Using APAC."),
            ]
        )
        agent = Agent(
            profile=AgentProfile(
                id="postgres-agent",
                instructions="Ask for the missing region.",
                tools=("request_region",),
            ),
            model=model,
            tools=(request_region,),
            run_store=store,
            event_store=store,
            checkpoint_store=store,
            artifact_store=store,
        )
        attachment = await store.add_attachment(
            name="context.txt",
            media_type="text/plain",
            content=b"durable context",
        )

        waiting = await agent.run("Build the report", attachments=(attachment,))
        run_id = UUID(waiting.metadata["run_id"])
        assert waiting.status is AgentResultStatus.WAITING
        assert (await store.get(run_id)).status is RunStatus.WAITING
        checkpoint = await store.load(run_id)
        assert checkpoint.attachments == (attachment,)

        claims = await asyncio.gather(
            store.claim(run_id), store.claim(run_id), return_exceptions=True
        )
        assert sum(isinstance(item, RuntimeCheckpoint) for item in claims) == 1
        assert sum(isinstance(item, CheckpointNotFoundError) for item in claims) == 1
        claimed = next(item for item in claims if isinstance(item, RuntimeCheckpoint))
        await store.save(claimed)

        waiting_events = await store.list(run_id)
        waiting_sequence = waiting_events[-1].sequence
        assert waiting_events[-1].type is EventType.RUN_WAITING
        assert [event async for event in store.subscribe(run_id)][-1].type is EventType.RUN_WAITING

        completed = await agent.resume(run_id, "APAC")
        resumed_events = [
            event
            async for event in store.subscribe(run_id, after_sequence=waiting_sequence)
        ]

        assert completed.status is AgentResultStatus.COMPLETED
        assert completed.output == "Using APAC."
        assert (await store.get(run_id)).status is RunStatus.COMPLETED
        assert resumed_events[-1].type is EventType.RUN_COMPLETED
        with pytest.raises(CheckpointNotFoundError):
            await store.load(run_id)


@requires_postgres
@pytest.mark.asyncio
async def test_conversation_turns_history_and_waiting_resume_are_durable() -> None:
    assert POSTGRES_DSN is not None
    model = FakeModel(
        [
            ModelResponse(content="Hello Xiao Ming."),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="region-call",
                        name="request_region",
                        arguments={"question": "Which region?"},
                    ),
                )
            ),
            ModelResponse(content="Using APAC."),
        ]
    )
    first_store = PostgresStore.from_url(POSTGRES_DSN, poll_interval=0.01)
    await first_store.create_schema()
    first_agent = Agent(
        profile=AgentProfile(
            id="postgres-conversation",
            instructions="Remember prior turns.",
            tools=("request_region",),
        ),
        model=model,
        tools=(request_region,),
        run_store=first_store,
        event_store=first_store,
        checkpoint_store=first_store,
        artifact_store=first_store,
        conversation_store=first_store,
    )
    try:
        assert isinstance(first_store, ConversationStore)
        conversation = await first_agent.create_conversation()
        await first_agent.run("My name is Xiao Ming.", conversation_id=conversation.id)
        waiting = await first_agent.run("Build report", conversation_id=conversation.id)
        waiting_run_id = UUID(waiting.metadata["run_id"])
        assert waiting.status is AgentResultStatus.WAITING
    finally:
        await first_store.close()

    reopened = PostgresStore.from_url(POSTGRES_DSN, poll_interval=0.01)
    resumed_agent = Agent(
        profile=AgentProfile(
            id="postgres-conversation",
            instructions="Remember prior turns.",
            tools=("request_region",),
        ),
        model=model,
        tools=(request_region,),
        run_store=reopened,
        event_store=reopened,
        checkpoint_store=reopened,
        artifact_store=reopened,
        conversation_store=reopened,
    )
    try:
        assert (await reopened.get_conversation(conversation.id)).active_run_id == waiting_run_id
        completed = await resumed_agent.resume(waiting_run_id, "APAC")
        turns = await reopened.list_turns(conversation.id)
        messages = await reopened.messages(conversation.id)

        assert completed.status is AgentResultStatus.COMPLETED
        assert [turn.sequence for turn in turns] == [1, 2]
        assert [turn.status for turn in turns] == [
            RunStatus.COMPLETED,
            RunStatus.COMPLETED,
        ]
        assert len(messages) == 4
        assert (await reopened.get_conversation(conversation.id)).active_run_id is None
        assert [message.content for message in model.requests[1].messages[:4]] == [
            "Remember prior turns.",
            "My name is Xiao Ming.",
            "Hello Xiao Ming.",
            "Build report",
        ]
    finally:
        await reopened.close()


@requires_postgres
@pytest.mark.asyncio
async def test_postgres_serializes_concurrent_conversation_turns() -> None:
    async for store in open_store():
        conversation = Conversation(profile_id="postgres-serial")
        await store.create_conversation(conversation)
        run_ids = (uuid4(), uuid4())

        results = await asyncio.gather(
            *(
                store.begin_turn(
                    conversation.id,
                    run_id=run_id,
                    profile_id="postgres-serial",
                    user_message="work",
                )
                for run_id in run_ids
            ),
            return_exceptions=True,
        )

        assert sum(not isinstance(result, Exception) for result in results) == 1
        assert sum(isinstance(result, ConversationBusyError) for result in results) == 1
