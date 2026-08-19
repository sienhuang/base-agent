import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy.schema import DropSchema

from base_agent import Agent, AgentDefinition, Flow, Stage
from base_agent.events import EventStore, EventStream
from base_agent.events.postgres import (
    EventPayloadSerializationError,
    PostgresEventStore,
    build_postgres_event_tables,
)
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
    assert {column.name for column in tables.events.primary_key} == {
        "run_id",
        "sequence",
    }
    with pytest.raises(ValueError, match="invalid PostgreSQL schema"):
        build_postgres_event_tables("agent-events;drop schema public")


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
