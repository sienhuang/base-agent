"""Optional PostgreSQL EventStore with polling replay subscriptions."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import BigInteger, Column, Integer, MetaData, String, Table, desc, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateSchema

from base_agent.models import Event

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TERMINAL_EVENT_KINDS = frozenset(
    {
        "flow.succeeded",
        "flow.failed",
        "flow.cancelled",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
    }
)


class EventPayloadSerializationError(ValueError):
    """Raised before persistence when an Event payload is not finite JSON."""


@dataclass(frozen=True, slots=True)
class PostgresEventTables:
    metadata: MetaData
    sequences: Table
    events: Table


def build_postgres_event_tables(schema: str | None = None) -> PostgresEventTables:
    """Build the isolated EventStore schema without performing database I/O."""
    if schema is not None and not _IDENTIFIER.fullmatch(schema):
        raise ValueError(f"invalid PostgreSQL schema name '{schema}'")
    metadata = MetaData(schema=schema)
    sequences = Table(
        "base_agent_event_sequences",
        metadata,
        Column("run_id", String(256), primary_key=True),
        Column("last_sequence", BigInteger, nullable=False),
    )
    events = Table(
        "base_agent_events",
        metadata,
        Column("run_id", String(256), primary_key=True),
        Column("sequence", BigInteger, primary_key=True),
        Column("kind", String(128), nullable=False),
        Column("turn", Integer, nullable=True),
        Column("payload", JSONB, nullable=False),
    )
    return PostgresEventTables(
        metadata=metadata,
        sequences=sequences,
        events=events,
    )


class PostgresEventStore:
    """Persist ordered Event facts and provide polling replay-then-follow."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str | None = None,
        poll_interval: float = 0.1,
        owns_engine: bool = False,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        self.engine = engine
        self.tables = build_postgres_event_tables(schema)
        self.schema = schema
        self.poll_interval = poll_interval
        self._owns_engine = owns_engine

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        poll_interval: float = 0.1,
        **engine_options: Any,
    ) -> PostgresEventStore:
        """Create an owned SQLAlchemy async engine from a PostgreSQL URL."""
        if not url.strip():
            raise ValueError("PostgreSQL URL must not be blank")
        engine = create_async_engine(_asyncpg_url(url), **engine_options)
        return cls(
            engine,
            schema=schema,
            poll_interval=poll_interval,
            owns_engine=True,
        )

    async def create_schema(self) -> None:
        """Create this adapter's schema and tables when they do not exist."""
        async with self.engine.begin() as connection:
            if self.schema is not None:
                await connection.execute(CreateSchema(self.schema, if_not_exists=True))
            await connection.run_sync(self.tables.metadata.create_all)

    async def close(self) -> None:
        """Dispose only an engine created by :meth:`from_url`."""
        if self._owns_engine:
            await self.engine.dispose()

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event:
        normalized_payload = _json_payload(payload or {})
        sequence_insert = postgresql_insert(self.tables.sequences).values(
            run_id=run_id,
            last_sequence=1,
        )
        allocate_sequence = sequence_insert.on_conflict_do_update(
            index_elements=[self.tables.sequences.c.run_id],
            set_={
                "last_sequence": self.tables.sequences.c.last_sequence + 1,
            },
        ).returning(self.tables.sequences.c.last_sequence)

        async with self.engine.begin() as connection:
            sequence = int(
                (await connection.execute(allocate_sequence)).scalar_one()
            )
            await connection.execute(
                self.tables.events.insert().values(
                    run_id=run_id,
                    sequence=sequence,
                    kind=kind,
                    turn=turn,
                    payload=normalized_payload,
                )
            )
        return Event(
            sequence=sequence,
            kind=kind,
            run_id=run_id,
            payload=normalized_payload,
            turn=turn,
        )

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]:
        _validate_cursor(after_sequence)
        statement = (
            select(
                self.tables.events.c.sequence,
                self.tables.events.c.kind,
                self.tables.events.c.turn,
                self.tables.events.c.payload,
            )
            .where(
                self.tables.events.c.run_id == run_id,
                self.tables.events.c.sequence > after_sequence,
            )
            .order_by(self.tables.events.c.sequence)
        )
        async with self.engine.connect() as connection:
            rows = (await connection.execute(statement)).all()
        return tuple(
            Event(
                sequence=int(row.sequence),
                kind=str(row.kind),
                run_id=run_id,
                payload=dict(row.payload),
                turn=(int(row.turn) if row.turn is not None else None),
            )
            for row in rows
        )

    async def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncGenerator[Event, None]:
        """Replay committed Events, then poll until a terminal Event is observed."""
        _validate_cursor(after_sequence)
        cursor = after_sequence
        while True:
            events = await self.list(run_id, after_sequence=cursor)
            for event in events:
                cursor = event.sequence
                yield event
                if event.kind in _TERMINAL_EVENT_KINDS:
                    return
            if not events and await self._is_terminal(run_id):
                return
            await asyncio.sleep(self.poll_interval)

    async def _is_terminal(self, run_id: str) -> bool:
        statement = (
            select(self.tables.events.c.kind)
            .where(self.tables.events.c.run_id == run_id)
            .order_by(desc(self.tables.events.c.sequence))
            .limit(1)
        )
        async with self.engine.connect() as connection:
            kind = (await connection.execute(statement)).scalar_one_or_none()
        return kind in _TERMINAL_EVENT_KINDS


def _asyncpg_url(url: str) -> str:
    if url.startswith("postgres://"):
        return f"postgresql+asyncpg://{url.removeprefix('postgres://')}"
    if url.startswith("postgresql://"):
        return f"postgresql+asyncpg://{url.removeprefix('postgresql://')}"
    return url


def _json_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        serialized = json.dumps(
            dict(payload),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise EventPayloadSerializationError(
            f"Event payload must be finite JSON: {exc}"
        ) from exc
    value = json.loads(serialized)
    if not isinstance(value, dict):
        raise EventPayloadSerializationError("Event payload must be a JSON object")
    return value


def _validate_cursor(after_sequence: int) -> None:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
