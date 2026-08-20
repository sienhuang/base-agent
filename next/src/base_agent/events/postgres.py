"""Optional PostgreSQL EventStore with polling replay subscriptions."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    desc,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.schema import CreateSchema

from base_agent.checkpoints import (
    CheckpointClaim,
    CheckpointClaimError,
    CheckpointConflictError,
    CheckpointNotFoundError,
    RuntimeCheckpoint,
    checkpoint_json,
)
from base_agent.models import Event

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TERMINAL_EVENT_KINDS = frozenset(
    {
        "flow.succeeded",
        "flow.failed",
        "flow.cancelled",
        "flow.waiting",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
        "run.waiting",
    }
)
_PERMANENT_TERMINAL_EVENT_KINDS = _TERMINAL_EVENT_KINDS - {
    "flow.waiting",
    "run.waiting",
}


class EventPayloadSerializationError(ValueError):
    """Raised before persistence when an Event payload is not finite JSON."""


@dataclass(frozen=True, slots=True)
class PostgresEventTables:
    metadata: MetaData
    sequences: Table
    events: Table
    checkpoints: Table


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
    checkpoints = Table(
        "base_agent_checkpoints",
        metadata,
        Column("run_id", String(256), primary_key=True),
        Column("agent_id", String(256), nullable=False, index=True),
        Column("revision", Integer, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("lease_token", String(64), nullable=True, unique=True),
        Column("lease_owner", String(512), nullable=True, index=True),
        Column("lease_expires_at", DateTime(timezone=True), nullable=True, index=True),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    return PostgresEventTables(
        metadata=metadata,
        sequences=sequences,
        events=events,
        checkpoints=checkpoints,
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
            for index, event in enumerate(events):
                cursor = event.sequence
                yield event
                if event.kind in _PERMANENT_TERMINAL_EVENT_KINDS:
                    return
                if (
                    event.kind in {"flow.waiting", "run.waiting"}
                    and index == len(events) - 1
                    and await self._is_terminal(run_id)
                ):
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


class PostgresCheckpointStore:
    """Lease-claimed RuntimeCheckpoint persistence sharing the EventStore schema."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str | None = None,
        owns_engine: bool = False,
    ) -> None:
        self.engine = engine
        self.tables = build_postgres_event_tables(schema)
        self.schema = schema
        self._owns_engine = owns_engine

    @classmethod
    def from_event_store(cls, event_store: PostgresEventStore) -> PostgresCheckpointStore:
        return cls(event_store.engine, schema=event_store.schema)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        **engine_options: Any,
    ) -> PostgresCheckpointStore:
        if not url.strip():
            raise ValueError("PostgreSQL URL must not be blank")
        return cls(
            create_async_engine(_asyncpg_url(url), **engine_options),
            schema=schema,
            owns_engine=True,
        )

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            if self.schema is not None:
                await connection.execute(CreateSchema(self.schema, if_not_exists=True))
            await connection.run_sync(self.tables.metadata.create_all)

    async def close(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    async def save(self, checkpoint: RuntimeCheckpoint) -> None:
        statement = (
            postgresql_insert(self.tables.checkpoints)
            .values(
                run_id=checkpoint.run_id,
                agent_id=checkpoint.agent_id,
                revision=checkpoint.revision,
                payload=checkpoint_json(checkpoint),
                updated_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=[self.tables.checkpoints.c.run_id])
            .returning(self.tables.checkpoints.c.run_id)
        )
        async with self.engine.begin() as connection:
            inserted = (await connection.execute(statement)).scalar_one_or_none()
        if inserted is None:
            raise CheckpointConflictError(
                f"checkpoint for run '{checkpoint.run_id}' already exists"
            )

    async def load(self, run_id: str) -> RuntimeCheckpoint:
        statement = select(self.tables.checkpoints.c.payload).where(
            self.tables.checkpoints.c.run_id == run_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise CheckpointNotFoundError(f"checkpoint for run '{run_id}' was not found")
        return RuntimeCheckpoint.model_validate(payload)

    async def claim(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> CheckpointClaim:
        if not owner_id.strip():
            raise ValueError("checkpoint owner_id must not be blank")
        if lease_seconds <= 0:
            raise ValueError("checkpoint lease_seconds must be greater than zero")
        now = datetime.now(UTC)
        token = str(uuid4())
        expires_at = now + timedelta(seconds=lease_seconds)
        statement = (
            update(self.tables.checkpoints)
            .where(
                self.tables.checkpoints.c.run_id == run_id,
                or_(
                    self.tables.checkpoints.c.lease_token.is_(None),
                    self.tables.checkpoints.c.lease_expires_at <= now,
                ),
            )
            .values(
                lease_token=token,
                lease_owner=owner_id,
                lease_expires_at=expires_at,
                updated_at=now,
            )
            .returning(self.tables.checkpoints.c.payload)
        )
        async with self.engine.begin() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            exists = await self._exists(run_id)
            if exists:
                raise CheckpointClaimError(
                    f"checkpoint for run '{run_id}' is already being resumed"
                )
            raise CheckpointNotFoundError(f"checkpoint for run '{run_id}' was not found")
        return CheckpointClaim(
            checkpoint=RuntimeCheckpoint.model_validate(payload),
            token=token,
            owner_id=owner_id,
            lease_expires_at=expires_at,
        )

    async def release(
        self,
        claim: CheckpointClaim,
        checkpoint: RuntimeCheckpoint,
    ) -> None:
        if claim.checkpoint.run_id != checkpoint.run_id:
            raise ValueError("claim and checkpoint run_id must match")
        statement = (
            update(self.tables.checkpoints)
            .where(
                self.tables.checkpoints.c.run_id == checkpoint.run_id,
                self.tables.checkpoints.c.lease_token == claim.token,
            )
            .values(
                agent_id=checkpoint.agent_id,
                revision=checkpoint.revision,
                payload=checkpoint_json(checkpoint),
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                updated_at=datetime.now(UTC),
            )
            .returning(self.tables.checkpoints.c.run_id)
        )
        async with self.engine.begin() as connection:
            updated = (await connection.execute(statement)).scalar_one_or_none()
        if updated is None:
            raise CheckpointConflictError("checkpoint claim is no longer current")

    async def renew(
        self,
        claim: CheckpointClaim,
        *,
        lease_seconds: float,
    ) -> CheckpointClaim:
        if lease_seconds <= 0:
            raise ValueError("checkpoint lease_seconds must be greater than zero")
        expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        statement = (
            update(self.tables.checkpoints)
            .where(
                self.tables.checkpoints.c.run_id == claim.checkpoint.run_id,
                self.tables.checkpoints.c.lease_token == claim.token,
            )
            .values(
                lease_expires_at=expires_at,
                updated_at=datetime.now(UTC),
            )
            .returning(self.tables.checkpoints.c.run_id)
        )
        async with self.engine.begin() as connection:
            renewed = (await connection.execute(statement)).scalar_one_or_none()
        if renewed is None:
            raise CheckpointConflictError("checkpoint claim is no longer current")
        return claim.model_copy(update={"lease_expires_at": expires_at})

    async def complete(self, claim: CheckpointClaim) -> None:
        statement = (
            delete(self.tables.checkpoints)
            .where(
                self.tables.checkpoints.c.run_id == claim.checkpoint.run_id,
                self.tables.checkpoints.c.lease_token == claim.token,
            )
            .returning(self.tables.checkpoints.c.run_id)
        )
        async with self.engine.begin() as connection:
            deleted = (await connection.execute(statement)).scalar_one_or_none()
        if deleted is None:
            raise CheckpointConflictError("checkpoint claim is no longer current")

    async def delete(self, run_id: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(self.tables.checkpoints).where(
                    self.tables.checkpoints.c.run_id == run_id
                )
            )

    async def _exists(self, run_id: str) -> bool:
        statement = select(self.tables.checkpoints.c.run_id).where(
            self.tables.checkpoints.c.run_id == run_id
        )
        async with self.engine.connect() as connection:
            return (await connection.execute(statement)).scalar_one_or_none() is not None


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
