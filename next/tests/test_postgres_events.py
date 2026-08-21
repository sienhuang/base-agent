import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import DropSchema

from base_agent import (
    Agent,
    AgentDefinition,
    CheckpointNotFoundError,
    Flow,
    Stage,
    WaitForInput,
    tool,
)
from base_agent.events import EventStore, EventStream
from base_agent.events.postgres import (
    EventPayloadSerializationError,
    PostgresCheckpointStore,
    PostgresEventStore,
    PostgresExecutionStore,
    PostgresRunStore,
    build_postgres_event_tables,
)
from base_agent.models import ModelResponse, RunStatus, ToolCall
from base_agent.run_store import AgentRunRecord, RunLeaseConflictError
from base_agent.testing import FakeModel

POSTGRES_DSN = os.getenv("BASE_AGENT_NEXT_TEST_POSTGRES_DSN")
requires_postgres = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set BASE_AGENT_NEXT_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)


def test_postgres_event_schema_and_configuration_are_validated() -> None:
    tables = build_postgres_event_tables("agent_events")

    assert tables.metadata.schema == "agent_events"
    assert tables.sequences.name == "base_agent_event_sequences"
    assert tables.events.name == "base_agent_events"
    assert tables.checkpoints.name == "base_agent_checkpoints"
    assert tables.runs.name == "base_agent_runs"
    assert tables.flow_runs.name == "base_agent_flow_runs"
    assert tables.flow_checkpoints.name == "base_agent_flow_checkpoints"
    assert tables.execution_requests.name == "base_agent_execution_requests"
    assert tables.execution_attempts.name == "base_agent_execution_attempts"
    assert tables.recovery_decisions.name == "base_agent_recovery_decisions"
    assert {column.name for column in tables.events.primary_key} == {
        "run_id",
        "sequence",
    }
    with pytest.raises(ValueError, match="invalid PostgreSQL schema"):
        build_postgres_event_tables("agent-events;drop schema public")


@requires_postgres
async def test_postgres_checkpoint_resumes_after_adapter_recreation() -> None:
    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    events = PostgresEventStore.from_url(POSTGRES_DSN, schema=schema, poll_interval=0.01)
    checkpoints = PostgresCheckpointStore.from_event_store(events)
    await events.create_schema()

    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?")

    definition = AgentDefinition(
        id="postgres-waiting",
        version="1.0.0",
        instructions="Ask and continue.",
        tools=("ask",),
        max_model_turns=2,
    )
    try:
        first = Agent(
            definition,
            model=FakeModel(
                [ModelResponse(tool_calls=(ToolCall(id="ask-1", name="ask", arguments={}),))]
            ),
            tools=(ask,),
            event_store=events,
            checkpoint_store=checkpoints,
        )
        waiting = await first.run("Begin")

        reopened_events = PostgresEventStore.from_url(
            POSTGRES_DSN,
            schema=schema,
            poll_interval=0.01,
        )
        reopened_checkpoints = PostgresCheckpointStore.from_event_store(reopened_events)
        try:
            recreated = Agent(
                definition,
                model=FakeModel([ModelResponse(content="continued")]),
                tools=(ask,),
                event_store=reopened_events,
                checkpoint_store=reopened_checkpoints,
            )
            completed = await recreated.resume(waiting.run_id, "yes")

            assert completed.text == "continued"
            with pytest.raises(CheckpointNotFoundError):
                await reopened_checkpoints.load(waiting.run_id)
        finally:
            await reopened_events.close()
    finally:
        async with events.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await events.close()


@requires_postgres
async def test_postgres_execution_store_fences_claims_and_requires_recovery() -> None:
    from base_agent.execution import (
        ExecutionLeaseLostError,
        ExecutionOperation,
        RecoveryDecision,
        RecoveryStrategy,
    )

    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    events = PostgresEventStore.from_url(POSTGRES_DSN, schema=schema)
    executions = PostgresExecutionStore.from_event_store(events)
    await events.create_schema()
    try:
        queued, _ = await executions.enqueue_run(
            AgentRunRecord(
                run_id="queued-run",
                agent_id="postgres-agent",
                agent_version="1.0.0",
                snapshot_fingerprint="snapshot-a",
                runtime_build_id="build-a",
            ),
            operation=ExecutionOperation.EXECUTE,
            payload={"prompt": "work"},
            idempotency_key="submit-queued-run",
        )
        claims = await asyncio.gather(
            executions.claim_next(
                owner_id="worker-a",
                runtime_route="default",
                runtime_build_id="build-a",
                lease_seconds=0.05,
            ),
            executions.claim_next(
                owner_id="worker-b",
                runtime_route="default",
                runtime_build_id="build-a",
                lease_seconds=0.05,
            ),
        )
        claim = next(item for item in claims if item is not None)
        assert queued.status is RunStatus.QUEUED
        assert sum(item is not None for item in claims) == 1

        await asyncio.sleep(0.06)
        transitions = await executions.reconcile_expired()
        recovery = await executions.runs.get("queued-run")
        assert len(transitions) == 1
        assert recovery.status is RunStatus.RECOVERY_REQUIRED
        with pytest.raises(ExecutionLeaseLostError):
            await executions.settle(claim.lease, status=RunStatus.SUCCEEDED)

        retried, _ = await executions.decide_recovery(
            "queued-run",
            decision=RecoveryDecision.RETRY,
            strategy=RecoveryStrategy.RESTART,
            subject_id="operator-1",
            reason_code="verified_safe",
            expected_revision=recovery.revision,
            idempotency_key="ticket-postgres-1",
        )
        assert retried.status is RunStatus.QUEUED
    finally:
        async with events.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await events.close()


@requires_postgres
async def test_postgres_run_store_persists_and_blocks_expired_owner() -> None:
    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    events = PostgresEventStore.from_url(POSTGRES_DSN, schema=schema)
    runs = PostgresRunStore.from_event_store(events)
    await events.create_schema()
    try:
        created = await runs.create(
            AgentRunRecord(
                run_id="durable-run",
                agent_id="postgres-agent",
                agent_version="1.0.0",
                snapshot_fingerprint="snapshot-a",
                runtime_build_id="build-a",
            )
        )
        lease = await runs.claim(
            created.run_id,
            owner_id="worker-a",
            lease_seconds=0.05,
        )

        reopened = PostgresRunStore.from_url(POSTGRES_DSN, schema=schema)
        try:
            running = await reopened.get(created.run_id)
            assert running.status is RunStatus.RUNNING
            assert running.lease_token == lease.token
            with pytest.raises(RunLeaseConflictError):
                await reopened.claim(
                    created.run_id,
                    owner_id="worker-b",
                    lease_seconds=30,
                )

            await asyncio.sleep(0.06)
            blocked = await reopened.block_expired()
            assert [record.run_id for record in blocked] == [created.run_id]
            assert (await runs.get(created.run_id)).status is RunStatus.BLOCKED
            assert await runs.block_expired() == ()
        finally:
            await reopened.close()
    finally:
        async with events.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await events.close()


@requires_postgres
async def test_postgres_event_store_is_durable_and_allocates_concurrent_sequences() -> None:
    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    store = PostgresEventStore.from_url(
        POSTGRES_DSN,
        schema=schema,
        poll_interval=0.01,
    )
    await store.create_schema()
    try:
        assert isinstance(store, EventStore)
        assert isinstance(store, EventStream)

        emitted = await asyncio.gather(
            *(store.emit("run-a", f"event.{index}") for index in range(50))
        )
        await store.emit("run-a", "run.succeeded", turn=2)
        await store.emit("run-b", "run.started")

        assert sorted(event.sequence for event in emitted) == list(range(1, 51))
        assert [event.sequence for event in await store.list("run-a")] == list(
            range(1, 52)
        )
        assert [event.sequence for event in await store.list("run-a", after_sequence=49)] == [
            50,
            51,
        ]
        assert [event.sequence for event in await store.list("run-b")] == [1]

        reopened = PostgresEventStore.from_url(
            POSTGRES_DSN,
            schema=schema,
            poll_interval=0.01,
        )
        try:
            replayed = tuple([event async for event in reopened.subscribe("run-a")])
            assert len(replayed) == 51
            assert replayed[-1].kind == "run.succeeded"
            assert tuple(
                [
                    event
                    async for event in reopened.subscribe(
                        "run-a",
                        after_sequence=replayed[-1].sequence,
                    )
                ]
            ) == ()
        finally:
            await reopened.close()
    finally:
        async with store.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await store.close()


@requires_postgres
async def test_postgres_event_store_replays_then_follows_new_events() -> None:
    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    store = PostgresEventStore.from_url(
        POSTGRES_DSN,
        schema=schema,
        poll_interval=0.01,
    )
    await store.create_schema()
    try:
        await store.emit("run-follow", "run.started")
        subscription = store.subscribe("run-follow")

        assert (await anext(subscription)).kind == "run.started"
        waiting = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0.03)
        terminal = await store.emit("run-follow", "run.cancelled", turn=1)

        assert await asyncio.wait_for(waiting, timeout=1) == terminal
        with pytest.raises(StopAsyncIteration):
            await anext(subscription)
    finally:
        async with store.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await store.close()


@requires_postgres
async def test_postgres_event_store_rejects_invalid_cursor_and_payload() -> None:
    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    store = PostgresEventStore.from_url(POSTGRES_DSN, schema=schema)
    await store.create_schema()
    try:
        with pytest.raises(ValueError, match="after_sequence must be non-negative"):
            await store.list("run-a", after_sequence=-1)
        with pytest.raises(EventPayloadSerializationError, match="finite JSON"):
            await store.emit("run-a", "bad.payload", {"value": float("nan")})
    finally:
        async with store.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await store.close()


@requires_postgres
async def test_postgres_event_store_persists_real_agent_and_flow_events() -> None:
    assert POSTGRES_DSN is not None
    schema = f"base_agent_next_{uuid4().hex}"
    store = PostgresEventStore.from_url(
        POSTGRES_DSN,
        schema=schema,
        poll_interval=0.01,
    )
    await store.create_schema()
    try:
        agent = Agent(
            AgentDefinition(
                id="postgres-agent",
                version="1.0.0",
                instructions="Answer briefly.",
            ),
            model=FakeModel.text("persisted answer"),
            event_store=store,
        )
        run = await agent.run("persist this Run")

        stage_agent = Agent(
            AgentDefinition(
                id="postgres-stage-agent",
                version="1.0.0",
                instructions="Complete this Stage.",
            ),
            model=FakeModel.text("persisted Flow output"),
        )
        flow = Flow.sequence(
            id="postgres-flow",
            stages=(Stage(name="only", agent=stage_agent),),
            event_store=store,
            forward_child_events=True,
        )
        flow_result = await flow.run("persist this Flow")

        reopened = PostgresEventStore.from_url(POSTGRES_DSN, schema=schema)
        try:
            assert await reopened.list(run.run_id) == run.events
            assert await reopened.list(flow_result.flow_run_id) == flow_result.events
        finally:
            await reopened.close()
    finally:
        async with store.engine.begin() as connection:
            await connection.execute(DropSchema(schema, cascade=True, if_exists=True))
        await store.close()
