# Optional PostgreSQL Persistence

`PostgresStore` is a durable implementation of the core `RunStore`, `EventStore`, `EventStream`,
`CheckpointStore`, `ArtifactStore`, and `ConversationStore` ports. `PostgresFlowRepository`
durably stores top-level Flow snapshots and their ordered lifecycle events. There is no
PostgreSQL-specific Agent or Flow subclass.

## Install

```bash
uv add 'base-agent[postgres]'
```

The adapter uses SQLAlchemy's async engine with the asyncpg driver. Importing `base_agent` does not
import either dependency; only `base_agent.stores.postgres` requires this extra.

## Use

```python
from base_agent import Agent
from base_agent.stores.postgres import PostgresStore

store = PostgresStore.from_url(
    "postgresql+asyncpg://agent:password@localhost/agents",
    poll_interval=0.1,
)
await store.create_schema()  # local development only

agent = Agent(
    profile=profile,
    model=model,
    tools=tools,
    run_store=store,
    event_store=store,
    checkpoint_store=store,
    artifact_store=store,
    conversation_store=store,
)

try:
    result = await agent.run("Prepare the report")
finally:
    await store.close()
```

For a Flow, share the Store's engine with the separate aggregate repository:

```python
from base_agent import FlowLifecycle
from base_agent.stores.postgres import PostgresFlowRepository

flow_repository = PostgresFlowRepository(store.engine)
flow_lifecycle = FlowLifecycle(flow_repository)

state = await flow_lifecycle.create(flow_definition)
state = await flow_lifecycle.start(state.run_id)
```

`PostgresFlowRepository.from_url(...)` is also available when a Flow repository owns its engine.
Call its `close()` when finished.

`create_schema()` creates missing tables and is intended for local development and tests. Use a
reviewed migration workflow in production. If `schema="agent_data"` is supplied, that PostgreSQL
schema must already exist.

## Durability and concurrency

- Run snapshots and immutable ordered events are stored as JSONB with indexed state columns.
- Flow snapshots live in `base_agent_flow_runs`; their events live in
  `base_agent_flow_events`. A row lock serializes writers, revision acts as a compare-and-swap
  guard, and each state transition commits its snapshot and events in one transaction.
- Flow execution ownership lives in `base_agent_flow_leases`. Claim, heartbeat, takeover, and
  release serialize on the Flow row. Each takeover increments `attempt` and replaces the random
  fencing token, so an expired worker cannot commit after a new worker takes ownership.
- Durable Flow work lives in `base_agent_flow_work_items`. Idempotency keys deduplicate enqueue;
  `FOR UPDATE SKIP LOCKED` distributes eligible rows across workers; delivery tokens fence
  completion and retry after timeout or redelivery.
- Operator decisions live in `base_agent_flow_work_reviews`. A review audit row and the BLOCKED
  WorkItem transition to PENDING or DISCARDED commit in one transaction; review idempotency and the
  WorkItem row lock prevent duplicate or conflicting decisions.
- Metadata-only external effect evidence lives in `base_agent_flow_side_effects`. The
  `(run_id, invocation_id, operation_key)` constraint deduplicates intent and row-locked revisions
  fence PREPARED/STARTED/CONFIRMED/ABORTED transitions. Business arguments and results are not
  stored in this table.
- Conversations and Run-backed Turns are stored in `base_agent_conversations` and
  `base_agent_conversation_turns`; row locking permits only one active Turn per Conversation.
- Emitting an event locks its Run row while assigning the next sequence number. Concurrent writers
  therefore produce one contiguous sequence per Run.
- Checkpoint `claim()` uses `DELETE ... RETURNING`; only one concurrent resume can acquire it.
- Attachments and Artifacts are stored as references in Run state and as binary content in BYTEA.
- `subscribe()` replays by sequence cursor and polls PostgreSQL until the Run reaches a waiting or
  terminal boundary.

BYTEA keeps this reference adapter self-contained and is suitable for modest payloads. Applications
with large files should implement `ArtifactStore` over object storage and keep only durable object
references in agent state.

The adapter does not provide authentication, tenant isolation, retention policies, backup policy,
distributed task scheduling, automatic heartbeats, recovery decisions, or automatic database
migrations. Those remain responsibilities of the host application and deployment.

## Integration test

Point the test suite at a disposable PostgreSQL database:

```bash
BASE_AGENT_TEST_POSTGRES_DSN='postgresql+asyncpg://postgres:password@localhost/base_agent' \
  uv run pytest tests/test_postgres.py
```

Without the environment variable, live database tests are skipped while schema validation remains
part of the normal offline suite.
