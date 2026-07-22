import asyncio
import os
from collections.abc import AsyncIterator
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelResponse,
    Run,
    RunStatus,
    RuntimeCheckpoint,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.postgres import PostgresStore
from base_agent.postgres.schema import build_tables
from base_agent.stores import (
    ArtifactStore,
    CheckpointNotFoundError,
    CheckpointStore,
    EventStore,
    EventStream,
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunStore,
)
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
    assert build_tables("agent_data").metadata.schema == "agent_data"
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
