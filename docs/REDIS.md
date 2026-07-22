# Optional Redis Event Notifications

`RedisEventStore` adds low-latency Pub/Sub notifications to any durable `EventStore`. It implements
both `EventStore` and `EventStream`, so it can be passed directly to `Agent` and the optional
FastAPI/SSE server.

Redis is not an event database in this design. Each event is committed to the wrapped store before
its sequence number is published. Subscribers treat a notification only as a hint and always read
the authoritative events from the durable store by sequence cursor.

## Install

```bash
uv add 'base-agent[redis]'
```

Importing `base_agent` does not import redis-py. Only `base_agent.redis` requires the extra.

## Combine Redis with PostgreSQL

```python
from base_agent import Agent
from base_agent.postgres import PostgresStore
from base_agent.redis import RedisEventStore

postgres = PostgresStore.from_url(
    "postgresql+asyncpg://agent:password@localhost/agents"
)
events = RedisEventStore.from_url(
    "redis://localhost:6379/0",
    durable_store=postgres,
    channel_prefix="my-service:agent-events",
    poll_interval=1.0,
)

agent = Agent(
    profile=profile,
    model=model,
    tools=tools,
    run_store=postgres,
    event_store=events,
    checkpoint_store=postgres,
    artifact_store=postgres,
)

try:
    result = await agent.run("Prepare the report")
finally:
    await events.close()
    await postgres.close()
```

Each process creates its own `RedisEventStore`, while all processes point at the same durable event
store and Redis deployment. `close()` only closes a Redis client created by `from_url()`; it never
closes the wrapped durable store.

## Delivery behavior

- `emit()` commits to the wrapped `EventStore` first.
- The Redis channel is namespaced per Run and carries only the committed sequence number.
- `subscribe()` replays all events after the requested cursor before waiting for notifications.
- After subscribing, it reconciles again to close the publish/subscribe race window.
- A Redis publishing failure is logged but does not turn a successfully persisted event into a
  failed Agent operation.
- A subscriber that cannot reach Redis polls the durable store at `poll_interval` and reconnects
  automatically.
- Waiting and terminal Run events preserve the same stream boundaries as other implementations.

Redis Pub/Sub is intentionally treated as at-most-once notification delivery. If Redis is down or a
subscriber is offline, cursor replay from PostgreSQL repairs the gap. Redis Streams are therefore
not required for this adapter; applications needing an independent durable queue should implement a
separate scheduling or messaging boundary.

## Production responsibilities

The host application owns Redis authentication and TLS, ACLs, connection-pool sizing, deployment
topology, monitoring, and channel-prefix isolation. Set socket/connect timeouts appropriate for the
deployment so fallback polling begins promptly during an outage.

## Integration test

Point the suite at a disposable Redis server:

```bash
BASE_AGENT_TEST_REDIS_URL='redis://localhost:6379/0' \
  uv run pytest tests/test_redis.py
```

Without the environment variable, the live Pub/Sub test is skipped; validation and Redis outage
fallback remain part of the offline suite.
