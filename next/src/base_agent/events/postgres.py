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
    Boolean,
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
from base_agent.models import Event, RunStatus
from base_agent.run_store import (
    AgentRunRecord,
    RunLease,
    RunLeaseConflictError,
    RunLeaseLostError,
    RunRecordAlreadyExistsError,
    RunRecordNotFoundError,
    RunTransitionError,
    run_record_json,
)

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
        "run.blocked",
        "run.interrupted",
    }
)
_PERMANENT_TERMINAL_EVENT_KINDS = _TERMINAL_EVENT_KINDS - {
    "flow.waiting",
    "run.waiting",
    "run.blocked",
}


class EventPayloadSerializationError(ValueError):
    """Raised before persistence when an Event payload is not finite JSON."""


@dataclass(frozen=True, slots=True)
class PostgresEventTables:
    metadata: MetaData
    sequences: Table
    events: Table
    checkpoints: Table
    runs: Table


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
    runs = Table(
        "base_agent_runs",
        metadata,
        Column("run_id", String(256), primary_key=True),
        Column("agent_id", String(256), nullable=False, index=True),
        Column("agent_version", String(128), nullable=False),
        Column("snapshot_fingerprint", String(128), nullable=False, index=True),
        Column("runtime_build_id", String(256), nullable=False, index=True),
        Column("status", String(32), nullable=False, index=True),
        Column("revision", Integer, nullable=False),
        Column("cancel_requested", Boolean, nullable=False),
        Column("lease_token", String(64), nullable=True, unique=True),
        Column("lease_owner", String(512), nullable=True, index=True),
        Column("lease_expires_at", DateTime(timezone=True), nullable=True, index=True),
        Column("heartbeat_at", DateTime(timezone=True), nullable=True),
        Column("payload", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    return PostgresEventTables(
        metadata=metadata,
        sequences=sequences,
        events=events,
        checkpoints=checkpoints,
        runs=runs,
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
                    event.kind in {"flow.waiting", "run.waiting", "run.blocked"}
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


class PostgresRunStore:
    """Lease-fenced Agent Run state sharing the EventStore engine and schema."""

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
    def from_event_store(cls, event_store: PostgresEventStore) -> PostgresRunStore:
        return cls(event_store.engine, schema=event_store.schema)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        **engine_options: Any,
    ) -> PostgresRunStore:
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

    async def create(self, record: AgentRunRecord) -> AgentRunRecord:
        payload = run_record_json(record)
        statement = (
            postgresql_insert(self.tables.runs)
            .values(**_run_columns(record, payload))
            .on_conflict_do_nothing(index_elements=[self.tables.runs.c.run_id])
            .returning(self.tables.runs.c.run_id)
        )
        async with self.engine.begin() as connection:
            inserted = (await connection.execute(statement)).scalar_one_or_none()
        if inserted is None:
            raise RunRecordAlreadyExistsError(f"Run record '{record.run_id}' already exists")
        return AgentRunRecord.model_validate(payload)

    async def get(self, run_id: str) -> AgentRunRecord:
        statement = select(self.tables.runs.c.payload).where(
            self.tables.runs.c.run_id == run_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise RunRecordNotFoundError(f"Run record '{run_id}' was not found")
        return AgentRunRecord.model_validate(payload)

    async def create_claimed(
        self,
        record: AgentRunRecord,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> RunLease:
        _validate_run_lease(owner_id, lease_seconds)
        if record.status is not RunStatus.CREATED:
            raise RunTransitionError("create_claimed requires a CREATED Run record")
        now = datetime.now(UTC)
        lease = RunLease(
            run_id=record.run_id,
            owner_id=owner_id,
            token=str(uuid4()),
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        running = AgentRunRecord.model_validate(
            {
                **record.model_dump(mode="json"),
                "status": RunStatus.RUNNING,
                "revision": record.revision + 1,
                "lease_owner": owner_id,
                "lease_token": lease.token,
                "lease_expires_at": lease.expires_at,
                "heartbeat_at": now,
                "updated_at": now,
            }
        )
        statement = (
            postgresql_insert(self.tables.runs)
            .values(**_run_columns(running))
            .on_conflict_do_nothing(index_elements=[self.tables.runs.c.run_id])
            .returning(self.tables.runs.c.run_id)
        )
        async with self.engine.begin() as connection:
            inserted = (await connection.execute(statement)).scalar_one_or_none()
        if inserted is None:
            raise RunRecordAlreadyExistsError(f"Run record '{record.run_id}' already exists")
        return lease

    async def claim(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> RunLease:
        _validate_run_lease(owner_id, lease_seconds)
        now = datetime.now(UTC)
        lease = RunLease(
            run_id=run_id,
            owner_id=owner_id,
            token=str(uuid4()),
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        async with self.engine.begin() as connection:
            record = await self._locked_record(connection, run_id)
            if record.status not in {RunStatus.CREATED, RunStatus.WAITING}:
                raise RunLeaseConflictError(
                    f"Run '{run_id}' with status '{record.status.value}' cannot be claimed"
                )
            updated = record.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "revision": record.revision + 1,
                    "lease_owner": owner_id,
                    "lease_token": lease.token,
                    "lease_expires_at": lease.expires_at,
                    "heartbeat_at": now,
                    "updated_at": now,
                    "error": None,
                }
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.run_id == run_id)
                .values(**_run_columns(updated))
            )
        return lease

    async def renew(self, lease: RunLease, *, lease_seconds: float) -> RunLease:
        _validate_run_lease(lease.owner_id, lease_seconds)
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            record = await self._locked_record(connection, lease.run_id)
            _validate_owned_run(record, lease, now)
            renewed = lease.model_copy(
                update={"expires_at": now + timedelta(seconds=lease_seconds)}
            )
            updated = record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "lease_expires_at": renewed.expires_at,
                    "heartbeat_at": now,
                    "updated_at": now,
                }
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.run_id == lease.run_id)
                .values(**_run_columns(updated))
            )
        return renewed

    async def settle(
        self,
        lease: RunLease,
        *,
        status: RunStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: dict[str, Any] | None = None,
        checkpoint_revision: int | None = None,
    ) -> AgentRunRecord:
        _validate_run_settlement(status, checkpoint_revision)
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            record = await self._locked_record(connection, lease.run_id)
            _validate_owned_run(record, lease, now)
            settled = AgentRunRecord.model_validate(
                {
                    **record.model_dump(mode="json"),
                    "status": status,
                    "revision": record.revision + 1,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "checkpoint_revision": checkpoint_revision,
                    "output": output,
                    "text": text,
                    "error": error,
                    "pending_input": pending_input,
                    "updated_at": now,
                }
            )
            payload = run_record_json(settled)
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.run_id == lease.run_id)
                .values(**_run_columns(settled, payload))
            )
        return settled

    async def request_cancel(self, run_id: str) -> AgentRunRecord:
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            record = await self._locked_record(connection, run_id)
            if record.status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.INTERRUPTED,
            } or record.cancel_requested:
                return record
            updated = record.model_copy(
                update={
                    "cancel_requested": True,
                    "revision": record.revision + 1,
                    "updated_at": now,
                }
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.run_id == run_id)
                .values(**_run_columns(updated))
            )
        return updated

    async def cancel_unowned(self, run_id: str, *, reason: str) -> AgentRunRecord:
        now = datetime.now(UTC)
        async with self.engine.begin() as connection:
            record = await self._locked_record(connection, run_id)
            if record.status is RunStatus.CANCELLED:
                return record
            if record.status is RunStatus.RUNNING:
                raise RunTransitionError(f"RUNNING Run '{run_id}' must be cancelled by its owner")
            if record.status in {
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.INTERRUPTED,
            }:
                raise RunTransitionError(
                    f"Run '{run_id}' already reached status '{record.status.value}'"
                )
            cancelled = AgentRunRecord.model_validate(
                {
                    **record.model_dump(mode="json"),
                    "status": RunStatus.CANCELLED,
                    "revision": record.revision + 1,
                    "cancel_requested": True,
                    "checkpoint_revision": None,
                    "pending_input": None,
                    "output": None,
                    "text": "",
                    "error": reason,
                    "updated_at": now,
                }
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.run_id == run_id)
                .values(**_run_columns(cancelled))
            )
        return cancelled

    async def block_expired(
        self,
        *,
        agent_id: str | None = None,
    ) -> tuple[AgentRunRecord, ...]:
        now = datetime.now(UTC)
        conditions = [
            self.tables.runs.c.status == RunStatus.RUNNING.value,
            self.tables.runs.c.lease_expires_at <= now,
        ]
        if agent_id is not None:
            conditions.append(self.tables.runs.c.agent_id == agent_id)
        statement = (
            select(self.tables.runs.c.payload)
            .where(*conditions)
            .with_for_update(skip_locked=True)
        )
        blocked: list[AgentRunRecord] = []
        async with self.engine.begin() as connection:
            rows = (await connection.execute(statement)).scalars().all()
            for payload in rows:
                record = AgentRunRecord.model_validate(payload)
                updated = AgentRunRecord.model_validate(
                    {
                        **record.model_dump(mode="json"),
                        "status": RunStatus.BLOCKED,
                        "revision": record.revision + 1,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "error": "Run owner lease expired; automatic replay is disabled",
                        "updated_at": now,
                    }
                )
                await connection.execute(
                    update(self.tables.runs)
                    .where(
                        self.tables.runs.c.run_id == record.run_id,
                        self.tables.runs.c.status == RunStatus.RUNNING.value,
                    )
                    .values(**_run_columns(updated))
                )
                blocked.append(updated)
        return tuple(blocked)

    async def _locked_record(self, connection: Any, run_id: str) -> AgentRunRecord:
        statement = (
            select(self.tables.runs.c.payload)
            .where(self.tables.runs.c.run_id == run_id)
            .with_for_update()
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise RunRecordNotFoundError(f"Run record '{run_id}' was not found")
        return AgentRunRecord.model_validate(payload)


def _run_columns(
    record: AgentRunRecord,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": record.run_id,
        "agent_id": record.agent_id,
        "agent_version": record.agent_version,
        "snapshot_fingerprint": record.snapshot_fingerprint,
        "runtime_build_id": record.runtime_build_id,
        "status": record.status.value,
        "revision": record.revision,
        "cancel_requested": record.cancel_requested,
        "lease_token": record.lease_token,
        "lease_owner": record.lease_owner,
        "lease_expires_at": record.lease_expires_at,
        "heartbeat_at": record.heartbeat_at,
        "payload": payload if payload is not None else run_record_json(record),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _validate_run_lease(owner_id: str, lease_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Run lease owner_id must not be blank")
    if lease_seconds <= 0:
        raise ValueError("Run lease_seconds must be greater than zero")


def _validate_owned_run(record: AgentRunRecord, lease: RunLease, now: datetime) -> None:
    if (
        record.status is not RunStatus.RUNNING
        or record.lease_owner != lease.owner_id
        or record.lease_token != lease.token
    ):
        raise RunLeaseLostError(f"Run lease for '{lease.run_id}' is no longer owned")
    if record.lease_expires_at is None or record.lease_expires_at <= now:
        raise RunLeaseLostError(f"Run lease for '{lease.run_id}' has expired")


def _validate_run_settlement(
    status: RunStatus,
    checkpoint_revision: int | None,
) -> None:
    if status not in {
        RunStatus.WAITING,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }:
        raise RunTransitionError(f"RUNNING Run cannot settle as '{status.value}'")
    if status is RunStatus.WAITING and checkpoint_revision is None:
        raise RunTransitionError("WAITING settlement requires checkpoint_revision")
    if status is not RunStatus.WAITING and checkpoint_revision is not None:
        raise RunTransitionError("only WAITING settlement may retain checkpoint_revision")


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
