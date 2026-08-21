"""Application-owned EventStore selection and lifecycle."""

from __future__ import annotations

from dataclasses import dataclass

from base_agent import (
    CheckpointStore,
    InMemoryCheckpointStore,
    InMemoryRunStore,
    RunStore,
)
from base_agent.events import EventStore, InMemoryEventStore
from base_agent.execution import ExecutionStore, InMemoryExecutionStore
from base_agent.flow_store import FlowRunStore, InMemoryFlowRunStore

from agent_app.config import Settings


@dataclass(frozen=True, slots=True)
class EventStoreRuntime:
    store: EventStore
    checkpoints: CheckpointStore
    runs: RunStore
    flows: FlowRunStore
    executions: ExecutionStore

    async def start(self) -> None:
        create_schema = getattr(self.store, "create_schema", None)
        if create_schema is not None:
            await create_schema()

    async def close(self) -> None:
        close = getattr(self.store, "close", None)
        if close is not None:
            await close()


def build_event_store_runtime(settings: Settings) -> EventStoreRuntime:
    """Build the configured Store without hiding infrastructure ownership."""
    if settings.event_store == "memory":
        events = InMemoryEventStore()
        runs = InMemoryRunStore()
        flows = InMemoryFlowRunStore()
        return EventStoreRuntime(
            events,
            InMemoryCheckpointStore(),
            runs,
            flows,
            InMemoryExecutionStore(
                runs,
                flow_store=flows,
                event_store=events,
            ),
        )
    if settings.postgres_dsn is None:
        raise ValueError(
            "AGENT_POSTGRES_DSN is required when AGENT_EVENT_STORE=postgres"
        )
    try:
        from base_agent.events.postgres import (
            PostgresCheckpointStore,
            PostgresEventStore,
            PostgresExecutionStore,
            PostgresRunStore,
        )
    except ImportError as exc:
        raise ImportError(
            "PostgreSQL EventStore requires the Starter 'postgres' extra"
        ) from exc
    event_store = PostgresEventStore.from_url(
        settings.postgres_dsn,
        schema=settings.postgres_schema,
        poll_interval=settings.event_poll_interval_seconds,
    )
    executions = PostgresExecutionStore.from_event_store(event_store)
    return EventStoreRuntime(
        event_store,
        PostgresCheckpointStore.from_event_store(event_store),
        PostgresRunStore.from_event_store(event_store),
        executions.flows,
        executions,
    )
