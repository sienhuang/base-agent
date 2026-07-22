import asyncio
import os
from uuid import UUID, uuid4

import pytest
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelResponse,
    RuntimeEvent,
)
from base_agent.redis import RedisEventStore
from base_agent.stores import EventStore, EventStream, InMemoryEventStore
from base_agent.testing import FakeModel

REDIS_URL = os.getenv("BASE_AGENT_TEST_REDIS_URL")
requires_redis = pytest.mark.skipif(
    REDIS_URL is None,
    reason="set BASE_AGENT_TEST_REDIS_URL to run Redis integration tests",
)


class UnavailableRedis:
    async def publish(self, channel: str, message: str) -> int:
        del channel, message
        raise RedisConnectionError("unavailable")


def test_redis_configuration_is_validated() -> None:
    durable = InMemoryEventStore()
    client = Redis.from_url("redis://localhost", decode_responses=True)
    with pytest.raises(ValueError, match="channel_prefix must not be blank"):
        RedisEventStore(durable, client, channel_prefix=" ")
    with pytest.raises(ValueError, match="must not contain whitespace"):
        RedisEventStore(durable, client, channel_prefix="base agent")
    with pytest.raises(ValueError, match="poll_interval"):
        RedisEventStore(durable, client, poll_interval=0)


@pytest.mark.asyncio
async def test_publish_failure_does_not_erase_durable_event() -> None:
    durable = InMemoryEventStore()
    store = RedisEventStore(durable, UnavailableRedis())  # type: ignore[arg-type]
    run_id = uuid4()

    event = await store.emit(run_id, EventType.RUN_STARTED)

    assert event.sequence == 1
    assert await store.list(run_id) == (event,)


@requires_redis
@pytest.mark.asyncio
async def test_redis_wakes_an_independent_subscriber_without_waiting_for_poll() -> None:
    assert REDIS_URL is not None
    durable = InMemoryEventStore()
    publisher = RedisEventStore.from_url(
        REDIS_URL,
        durable,
        channel_prefix="base-agent:test",
        poll_interval=5,
    )
    subscriber = RedisEventStore.from_url(
        REDIS_URL,
        durable,
        channel_prefix="base-agent:test",
        poll_interval=5,
    )
    try:
        assert isinstance(publisher, EventStore)
        assert isinstance(subscriber, EventStream)
        run_id = uuid4()
        agent = Agent(
            profile=AgentProfile(id="redis-agent", instructions="Complete the task."),
            model=FakeModel([ModelResponse(content="done")]),
            event_store=publisher,
        )
        collecting = asyncio.create_task(
            collect_events(subscriber, run_id, after_sequence=0)
        )
        await asyncio.sleep(0.05)

        result = await agent.run("Complete", run_id=run_id)
        received = await asyncio.wait_for(collecting, timeout=1)

        assert result.status is AgentResultStatus.COMPLETED
        assert received == list(await durable.list(run_id))
        assert received[-1].type is EventType.RUN_COMPLETED
    finally:
        await publisher.close()
        await subscriber.close()


@pytest.mark.asyncio
async def test_subscriber_polls_durable_history_when_redis_is_unavailable() -> None:
    durable = InMemoryEventStore()
    subscriber = RedisEventStore.from_url(
        "redis://127.0.0.1:1",
        durable,
        poll_interval=0.01,
        socket_connect_timeout=0.02,
        socket_timeout=0.02,
    )
    run_id = uuid4()
    try:
        collecting = asyncio.create_task(
            collect_events(subscriber, run_id, after_sequence=0)
        )
        await asyncio.sleep(0.05)
        emitted = await durable.emit(run_id, EventType.RUN_COMPLETED)

        received = await asyncio.wait_for(collecting, timeout=1)

        assert received == [emitted]
    finally:
        await subscriber.close()


async def collect_events(
    store: RedisEventStore,
    run_id: UUID,
    *,
    after_sequence: int,
) -> list[RuntimeEvent]:
    return [
        event
        async for event in store.subscribe(run_id, after_sequence=after_sequence)
    ]
