# Optional PostgreSQL Persistence

`PostgresStore` is a durable implementation of the core `RunStore`, `EventStore`, `EventStream`,
`CheckpointStore`, and `ArtifactStore` ports. The same instance can be passed directly to `Agent`;
there is no PostgreSQL-specific Agent subclass or application adapter.

## Install

```bash
uv add 'base-agent[postgres]'
```

The adapter uses SQLAlchemy's async engine with the asyncpg driver. Importing `base_agent` does not
import either dependency; only `base_agent.postgres` requires this extra.

## Use

```python
from base_agent import Agent
from base_agent.postgres import PostgresStore

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
)

try:
    result = await agent.run("Prepare the report")
finally:
    await store.close()
```

`create_schema()` creates missing tables and is intended for local development and tests. Use a
reviewed migration workflow in production. If `schema="agent_data"` is supplied, that PostgreSQL
schema must already exist.

## Durability and concurrency

- Run snapshots and immutable ordered events are stored as JSONB with indexed state columns.
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
distributed task scheduling, or automatic database migrations. Those remain responsibilities of
the host application and deployment.

## Integration test

Point the test suite at a disposable PostgreSQL database:

```bash
BASE_AGENT_TEST_POSTGRES_DSN='postgresql+asyncpg://postgres:password@localhost/base_agent' \
  uv run pytest tests/test_postgres.py
```

Without the environment variable, live database tests are skipped while schema validation remains
part of the normal offline suite.
