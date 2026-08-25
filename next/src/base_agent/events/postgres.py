"""Optional PostgreSQL EventStore with polling replay subscriptions."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    delete,
    desc,
    func,
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
from base_agent.execution import (
    ArtifactReceipt,
    ArtifactReceiptStatus,
    AttemptHeartbeat,
    ClaimedExecution,
    ExecutionAttempt,
    ExecutionAttemptNotFoundError,
    ExecutionAttemptStatus,
    ExecutionLease,
    ExecutionLeaseLostError,
    ExecutionOperation,
    ExecutionRequest,
    ExecutionRequestConflictError,
    ExecutionRequestStatus,
    ExecutionTargetKind,
    RecoveryDecision,
    RecoveryDecisionRecord,
    RecoveryEvidenceSnapshot,
    RecoveryGateBlockedError,
    RecoveryStrategy,
    RecoveryTransition,
    SideEffectEvidence,
    SideEffectStatus,
    _artifact_receipt_from_artifact,
    _artifact_receipt_from_event,
    _checkpoint_artifacts,
    _derived_idempotency_key,
    _recovery_gate_error,
    _recovery_strategy_blockers,
    _resumable_side_effect_evidence,
    _side_effect_completion_status,
    _side_effect_evidence_id,
    _side_effect_started_from_event,
    _validate_artifact_receipt_intent,
    _validate_recovery_intent,
    _validate_side_effect_intent,
)
from base_agent.flow_store import (
    FlowCheckpoint,
    FlowCheckpointConflictError,
    FlowCheckpointNotFoundError,
    FlowRunAlreadyExistsError,
    FlowRunRecord,
    FlowRunRecordNotFoundError,
    FlowTransitionError,
    flow_checkpoint_json,
    flow_record_json,
)
from base_agent.flows.models import FlowStatus
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
        "flow.blocked",
        "flow.recovery_required",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
        "run.waiting",
        "run.blocked",
        "run.interrupted",
        "run.recovery_required",
    }
)
_PERMANENT_TERMINAL_EVENT_KINDS = _TERMINAL_EVENT_KINDS - {
    "flow.waiting",
    "flow.blocked",
    "flow.recovery_required",
    "run.waiting",
    "run.blocked",
    "run.recovery_required",
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
    flow_runs: Table
    flow_checkpoints: Table
    execution_requests: Table
    execution_attempts: Table
    recovery_decisions: Table
    artifact_receipts: Table
    side_effect_evidence: Table


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
    flow_runs = Table(
        "base_agent_flow_runs",
        metadata,
        Column("flow_run_id", String(256), primary_key=True),
        Column("flow_name", String(256), nullable=False, index=True),
        Column("flow_id", String(256), nullable=False, index=True),
        Column("flow_kind", String(32), nullable=False, index=True),
        Column("definition_fingerprint", String(128), nullable=False, index=True),
        Column("runtime_build_id", String(256), nullable=False, index=True),
        Column("runtime_route", String(256), nullable=False, index=True),
        Column("status", String(32), nullable=False, index=True),
        Column("revision", Integer, nullable=False),
        Column("cancel_requested", Boolean, nullable=False),
        Column("current_request_id", String(64), nullable=True, index=True),
        Column("current_attempt_id", String(64), nullable=True, index=True),
        Column("checkpoint_revision", Integer, nullable=True),
        Column("payload", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    flow_checkpoints = Table(
        "base_agent_flow_checkpoints",
        metadata,
        Column("flow_run_id", String(256), primary_key=True),
        Column("flow_id", String(256), nullable=False, index=True),
        Column("flow_kind", String(32), nullable=False, index=True),
        Column("definition_fingerprint", String(128), nullable=False, index=True),
        Column("revision", Integer, nullable=False),
        Column("payload", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    execution_requests = Table(
        "base_agent_execution_requests",
        metadata,
        Column("request_id", String(64), primary_key=True),
        Column("target_kind", String(32), nullable=False, index=True),
        Column("target_id", String(256), nullable=False, index=True),
        Column("operation", String(32), nullable=False),
        Column("status", String(32), nullable=False, index=True),
        Column("runtime_route", String(256), nullable=False, index=True),
        Column("idempotency_key", String(256), nullable=False, unique=True),
        Column("expected_target_revision", Integer, nullable=False),
        Column("available_at", DateTime(timezone=True), nullable=False, index=True),
        Column("claimed_attempt_id", String(64), nullable=True, index=True),
        Column("record", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        Index(
            "ix_base_agent_execution_requests_claim",
            "status",
            "runtime_route",
            "available_at",
        ),
    )
    execution_attempts = Table(
        "base_agent_execution_attempts",
        metadata,
        Column("attempt_id", String(64), primary_key=True),
        Column("target_kind", String(32), nullable=False, index=True),
        Column("target_id", String(256), nullable=False, index=True),
        Column("request_id", String(64), nullable=False, index=True),
        Column("attempt_number", Integer, nullable=False),
        Column("status", String(32), nullable=False, index=True),
        Column("owner_id", String(512), nullable=False, index=True),
        Column("fencing_token", String(64), nullable=False, unique=True),
        Column("runtime_build_id", String(256), nullable=False, index=True),
        Column("heartbeat_at", DateTime(timezone=True), nullable=False),
        Column("lease_expires_at", DateTime(timezone=True), nullable=False, index=True),
        Column("record", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "target_kind",
            "target_id",
            "attempt_number",
            name="uq_base_agent_attempt_number",
        ),
        Index(
            "ix_base_agent_execution_attempts_reconcile",
            "status",
            "lease_expires_at",
        ),
    )
    recovery_decisions = Table(
        "base_agent_recovery_decisions",
        metadata,
        Column("decision_id", String(64), primary_key=True),
        Column("target_kind", String(32), nullable=False, index=True),
        Column("target_id", String(256), nullable=False, index=True),
        Column("decision", String(32), nullable=False),
        Column("idempotency_key", String(256), nullable=False, unique=True),
        Column("record", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    artifact_receipts = Table(
        "base_agent_artifact_receipts",
        metadata,
        Column("artifact_id", String(256), primary_key=True),
        Column("target_kind", String(32), nullable=False, index=True),
        Column("target_id", String(256), nullable=False, index=True),
        Column("execution_attempt_id", String(64), nullable=False, index=True),
        Column("status", String(32), nullable=False, index=True),
        Column("checkpoint_revision", Integer, nullable=True, index=True),
        Column("record", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
    )
    side_effect_evidence = Table(
        "base_agent_side_effect_evidence",
        metadata,
        Column("evidence_id", String(80), primary_key=True),
        Column("target_kind", String(32), nullable=False, index=True),
        Column("target_id", String(256), nullable=False, index=True),
        Column("execution_attempt_id", String(64), nullable=False, index=True),
        Column("operation_id", String(256), nullable=False, index=True),
        Column("side_effect", String(32), nullable=False, index=True),
        Column("status", String(32), nullable=False, index=True),
        Column("checkpoint_revision", Integer, nullable=True, index=True),
        Column("record", JSONB, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("updated_at", DateTime(timezone=True), nullable=False),
        UniqueConstraint(
            "operation_id",
            "execution_attempt_id",
            name="uq_base_agent_side_effect_operation_attempt",
        ),
    )
    return PostgresEventTables(
        metadata=metadata,
        sequences=sequences,
        events=events,
        checkpoints=checkpoints,
        runs=runs,
        flow_runs=flow_runs,
        flow_checkpoints=flow_checkpoints,
        execution_requests=execution_requests,
        execution_attempts=execution_attempts,
        recovery_decisions=recovery_decisions,
        artifact_receipts=artifact_receipts,
        side_effect_evidence=side_effect_evidence,
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
        async with self.engine.begin() as connection:
            sequence = await self._append_event(
                connection,
                run_id,
                kind,
                normalized_payload,
                turn=turn,
            )
        return Event(
            sequence=sequence,
            kind=kind,
            run_id=run_id,
            payload=normalized_payload,
            turn=turn,
        )

    async def emit_fenced(
        self,
        lease: ExecutionLease,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event:
        """Validate an Attempt token and append its Event in one transaction."""
        normalized_payload = _json_payload(payload or {})
        async with self.engine.begin() as connection:
            statement = (
                select(self.tables.execution_attempts.c.record)
                .where(self.tables.execution_attempts.c.attempt_id == lease.attempt_id)
                .with_for_update()
            )
            attempt_payload = (await connection.execute(statement)).scalar_one_or_none()
            if attempt_payload is None:
                raise ExecutionAttemptNotFoundError(
                    f"ExecutionAttempt '{lease.attempt_id}' was not found"
                )
            attempt = ExecutionAttempt.model_validate(attempt_payload)
            now = await _database_now(connection)
            if (
                attempt.status not in _ACTIVE_EXECUTION_ATTEMPT_STATUSES
                or attempt.owner_id != lease.owner_id
                or attempt.fencing_token != lease.fencing_token
                or attempt.request_id != lease.request_id
                or attempt.target_id != lease.target_id
                or attempt.lease_expires_at <= now
            ):
                raise ExecutionLeaseLostError(
                    f"ExecutionAttempt lease '{lease.attempt_id}' is no longer live"
                )
            target_table = (
                self.tables.runs
                if attempt.target_kind is ExecutionTargetKind.AGENT_RUN
                else self.tables.flow_runs
            )
            target_column = (
                target_table.c.run_id
                if attempt.target_kind is ExecutionTargetKind.AGENT_RUN
                else target_table.c.flow_run_id
            )
            target_payload = (
                await connection.execute(
                    select(target_table.c.payload)
                    .where(target_column == attempt.target_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if target_payload is None:
                raise ExecutionLeaseLostError("Execution target no longer exists")
            target = (
                AgentRunRecord.model_validate(target_payload)
                if attempt.target_kind is ExecutionTargetKind.AGENT_RUN
                else FlowRunRecord.model_validate(target_payload)
            )
            _validate_current_attempt_target(attempt, target)
            is_flow_child_event = (
                attempt.target_kind is ExecutionTargetKind.FLOW_RUN
                and normalized_payload.get("execution_target_id") == lease.target_id
            )
            if run_id != lease.target_id and not is_flow_child_event:
                raise ExecutionLeaseLostError(
                    "Attempt lease target does not match Event run_id"
                )
            await _record_event_evidence_in_transaction(
                connection,
                self.tables,
                attempt,
                kind,
                normalized_payload,
                turn=turn,
                now=now,
            )
            sequence = await self._append_event(
                connection,
                run_id,
                kind,
                normalized_payload,
                turn=turn,
            )
        return Event(
            sequence=sequence,
            kind=kind,
            run_id=run_id,
            payload=normalized_payload,
            turn=turn,
        )

    async def _append_event(
        self,
        connection: Any,
        run_id: str,
        kind: str,
        normalized_payload: Mapping[str, Any],
        *,
        turn: int | None,
    ) -> int:
        return await _append_event_in_transaction(
            connection,
            self.tables,
            run_id,
            kind,
            normalized_payload,
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

    async def transition(
        self,
        run_id: str,
        *,
        expected_statuses: Collection[RunStatus],
        status: RunStatus,
        expected_revision: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> AgentRunRecord:
        async with self.engine.begin() as connection:
            record = await self._locked_record(connection, run_id)
            if record.status not in expected_statuses:
                raise RunTransitionError(
                    f"Run '{run_id}' with status '{record.status.value}' cannot "
                    f"transition to '{status.value}'"
                )
            if expected_revision is not None and record.revision != expected_revision:
                raise RunTransitionError(
                    f"Run '{run_id}' revision changed from {expected_revision} "
                    f"to {record.revision}"
                )
            now = await _database_now(connection)
            values: dict[str, Any] = {
                "status": status,
                "revision": record.revision + 1,
                "updated_at": now,
                **dict(updates or {}),
            }
            if status is not RunStatus.RUNNING:
                values.update(
                    lease_owner=None,
                    lease_token=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            transitioned = AgentRunRecord.model_validate(
                record.model_copy(update=values).model_dump(mode="json")
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.run_id == run_id)
                .values(**_run_columns(transitioned))
            )
        return transitioned

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


class PostgresFlowRunStore:
    """PostgreSQL lifecycle and strategy checkpoint store for top-level Flows."""

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
    def from_event_store(cls, event_store: PostgresEventStore) -> PostgresFlowRunStore:
        return cls(event_store.engine, schema=event_store.schema)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        **engine_options: Any,
    ) -> PostgresFlowRunStore:
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

    async def create(self, record: FlowRunRecord) -> FlowRunRecord:
        payload = flow_record_json(record)
        statement = (
            postgresql_insert(self.tables.flow_runs)
            .values(**_flow_run_columns(record, payload))
            .on_conflict_do_nothing(
                index_elements=[self.tables.flow_runs.c.flow_run_id]
            )
            .returning(self.tables.flow_runs.c.flow_run_id)
        )
        async with self.engine.begin() as connection:
            inserted = (await connection.execute(statement)).scalar_one_or_none()
        if inserted is None:
            raise FlowRunAlreadyExistsError(
                f"Flow Run '{record.flow_run_id}' already exists"
            )
        return FlowRunRecord.model_validate(payload)

    async def get(self, flow_run_id: str) -> FlowRunRecord:
        statement = select(self.tables.flow_runs.c.payload).where(
            self.tables.flow_runs.c.flow_run_id == flow_run_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise FlowRunRecordNotFoundError(
                f"Flow Run '{flow_run_id}' was not found"
            )
        return FlowRunRecord.model_validate(payload)

    async def transition(
        self,
        flow_run_id: str,
        *,
        expected_statuses: Collection[FlowStatus],
        status: FlowStatus,
        expected_revision: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> FlowRunRecord:
        async with self.engine.begin() as connection:
            current = await self._locked_record(connection, flow_run_id)
            if current.status not in expected_statuses:
                raise FlowTransitionError(
                    f"Flow Run '{flow_run_id}' with status '{current.status.value}' "
                    f"cannot transition to '{status.value}'"
                )
            if expected_revision is not None and current.revision != expected_revision:
                raise FlowTransitionError(
                    f"Flow Run '{flow_run_id}' revision changed from "
                    f"{expected_revision} to {current.revision}"
                )
            now = await _database_now(connection)
            updated = FlowRunRecord.model_validate(
                {
                    **current.model_dump(mode="json"),
                    "status": status,
                    "revision": current.revision + 1,
                    "updated_at": now,
                    **dict(updates or {}),
                }
            )
            await self._write_record(connection, updated)
        return updated

    async def save(
        self,
        checkpoint: FlowCheckpoint,
        *,
        expected_revision: int | None,
    ) -> FlowCheckpoint:
        normalized = FlowCheckpoint.model_validate(flow_checkpoint_json(checkpoint))
        async with self.engine.begin() as connection:
            run = await self._locked_record(connection, checkpoint.flow_run_id)
            if (
                run.flow_id != checkpoint.flow_id
                or run.flow_kind is not checkpoint.flow_kind
                or run.definition_fingerprint != checkpoint.definition_fingerprint
            ):
                raise FlowCheckpointConflictError(
                    "Flow checkpoint does not match its pinned Flow definition"
                )
            statement = (
                select(self.tables.flow_checkpoints.c.payload)
                .where(
                    self.tables.flow_checkpoints.c.flow_run_id
                    == checkpoint.flow_run_id
                )
                .with_for_update()
            )
            payload = (await connection.execute(statement)).scalar_one_or_none()
            current = (
                None if payload is None else FlowCheckpoint.model_validate(payload)
            )
            current_revision = current.revision if current is not None else None
            if current_revision != expected_revision:
                raise FlowCheckpointConflictError(
                    f"Flow checkpoint '{checkpoint.flow_run_id}' revision changed from "
                    f"{expected_revision} to {current_revision}"
                )
            expected_next = 1 if current is None else current.revision + 1
            if checkpoint.revision != expected_next:
                raise FlowCheckpointConflictError(
                    f"Flow checkpoint revision must advance to {expected_next}"
                )
            if current is None:
                await connection.execute(
                    postgresql_insert(self.tables.flow_checkpoints).values(
                        **_flow_checkpoint_columns(normalized)
                    )
                )
            else:
                await connection.execute(
                    update(self.tables.flow_checkpoints)
                    .where(
                        self.tables.flow_checkpoints.c.flow_run_id
                        == checkpoint.flow_run_id
                    )
                    .values(**_flow_checkpoint_columns(normalized))
                )
        return normalized

    async def load(self, flow_run_id: str) -> FlowCheckpoint:
        statement = select(self.tables.flow_checkpoints.c.payload).where(
            self.tables.flow_checkpoints.c.flow_run_id == flow_run_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise FlowCheckpointNotFoundError(
                f"Flow checkpoint '{flow_run_id}' was not found"
            )
        return FlowCheckpoint.model_validate(payload)

    async def delete(self, flow_run_id: str) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(self.tables.flow_checkpoints).where(
                    self.tables.flow_checkpoints.c.flow_run_id == flow_run_id
                )
            )

    async def _locked_record(
        self,
        connection: Any,
        flow_run_id: str,
    ) -> FlowRunRecord:
        statement = (
            select(self.tables.flow_runs.c.payload)
            .where(self.tables.flow_runs.c.flow_run_id == flow_run_id)
            .with_for_update()
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise FlowRunRecordNotFoundError(
                f"Flow Run '{flow_run_id}' was not found"
            )
        return FlowRunRecord.model_validate(payload)

    async def _write_record(self, connection: Any, record: FlowRunRecord) -> None:
        await connection.execute(
            update(self.tables.flow_runs)
            .where(self.tables.flow_runs.c.flow_run_id == record.flow_run_id)
            .values(**_flow_run_columns(record))
        )


class PostgresExecutionStore:
    """PostgreSQL queue and Attempt leases with database-time fencing."""

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
        self.runs = PostgresRunStore(engine, schema=schema)
        self.flows = PostgresFlowRunStore(engine, schema=schema)

    @classmethod
    def from_event_store(
        cls,
        event_store: PostgresEventStore,
    ) -> PostgresExecutionStore:
        return cls(event_store.engine, schema=event_store.schema)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        **engine_options: Any,
    ) -> PostgresExecutionStore:
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

    async def enqueue_run(
        self,
        record: AgentRunRecord,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, ExecutionRequest]:
        if operation is not ExecutionOperation.EXECUTE:
            raise ValueError("a new Run requires the execute operation")
        async with self.engine.begin() as connection:
            existing = await self._request_by_key(connection, idempotency_key)
            if existing is not None:
                self._validate_request_intent(existing, record.run_id, operation, payload)
                return await self.runs._locked_record(connection, record.run_id), existing
            now = await _database_now(connection)
            request = ExecutionRequest(
                target_id=record.run_id,
                operation=operation,
                payload=_finite_json_mapping(payload),
                runtime_route=record.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=record.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = AgentRunRecord.model_validate(
                {
                    **record.model_dump(mode="json"),
                    "status": RunStatus.QUEUED,
                    "revision": record.revision + 1,
                    "current_request_id": request.request_id,
                    "current_attempt_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            inserted_request = (
                await connection.execute(
                    postgresql_insert(self.tables.execution_requests)
                    .values(**_execution_request_columns(request))
                    .on_conflict_do_nothing(
                        index_elements=[self.tables.execution_requests.c.idempotency_key]
                    )
                    .returning(self.tables.execution_requests.c.request_id)
                )
            ).scalar_one_or_none()
            if inserted_request is None:
                concurrent = await self._request_by_key(connection, idempotency_key)
                if concurrent is None:
                    raise ExecutionRequestConflictError(
                        "idempotent ExecutionRequest disappeared during submission"
                    )
                self._validate_request_intent(concurrent, record.run_id, operation, payload)
                return await self.runs._locked_record(connection, record.run_id), concurrent
            inserted = (
                await connection.execute(
                    postgresql_insert(self.tables.runs)
                    .values(**_run_columns(queued))
                    .on_conflict_do_nothing(index_elements=[self.tables.runs.c.run_id])
                    .returning(self.tables.runs.c.run_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                raise RunRecordAlreadyExistsError(
                    f"Run record '{record.run_id}' already exists"
                )
        return queued, request

    async def enqueue_continuation(
        self,
        run_id: str,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, ExecutionRequest]:
        if operation not in {ExecutionOperation.RESUME, ExecutionOperation.CONFIRM}:
            raise ValueError("continuation operation must be resume or confirm")
        async with self.engine.begin() as connection:
            existing = await self._request_by_key(connection, idempotency_key)
            if existing is not None:
                self._validate_request_intent(existing, run_id, operation, payload)
                return await self.runs._locked_record(connection, run_id), existing
            run = await self.runs._locked_record(connection, run_id)
            if run.status is not RunStatus.WAITING:
                raise RunTransitionError(
                    f"Run '{run_id}' cannot continue from status '{run.status.value}'"
                )
            now = await _database_now(connection)
            request = ExecutionRequest(
                target_id=run_id,
                operation=operation,
                payload=_finite_json_mapping(payload),
                runtime_route=run.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=run.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = AgentRunRecord.model_validate(
                {
                    **run.model_dump(mode="json"),
                    "status": RunStatus.QUEUED,
                    "revision": run.revision + 1,
                    "current_request_id": request.request_id,
                    "updated_at": now,
                }
            )
            await self._write_run(connection, queued)
            await connection.execute(
                postgresql_insert(self.tables.execution_requests).values(
                    **_execution_request_columns(request)
                )
            )
        return queued, request

    async def enqueue_flow(
        self,
        record: FlowRunRecord,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, ExecutionRequest]:
        if operation is not ExecutionOperation.EXECUTE:
            raise ValueError("a new Flow Run requires the execute operation")
        async with self.engine.begin() as connection:
            existing = await self._request_by_key(connection, idempotency_key)
            if existing is not None:
                self._validate_request_intent(
                    existing,
                    record.flow_run_id,
                    operation,
                    payload,
                    target_kind=ExecutionTargetKind.FLOW_RUN,
                )
                return (
                    await self.flows._locked_record(connection, record.flow_run_id),
                    existing,
                )
            now = await _database_now(connection)
            request = ExecutionRequest(
                target_kind=ExecutionTargetKind.FLOW_RUN,
                target_id=record.flow_run_id,
                operation=operation,
                payload=_finite_json_mapping(payload),
                runtime_route=record.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=record.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = FlowRunRecord.model_validate(
                {
                    **record.model_dump(mode="json"),
                    "status": FlowStatus.QUEUED,
                    "revision": record.revision + 1,
                    "current_request_id": request.request_id,
                    "current_attempt_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            inserted_request = (
                await connection.execute(
                    postgresql_insert(self.tables.execution_requests)
                    .values(**_execution_request_columns(request))
                    .on_conflict_do_nothing(
                        index_elements=[self.tables.execution_requests.c.idempotency_key]
                    )
                    .returning(self.tables.execution_requests.c.request_id)
                )
            ).scalar_one_or_none()
            if inserted_request is None:
                concurrent = await self._request_by_key(connection, idempotency_key)
                if concurrent is None:
                    raise ExecutionRequestConflictError(
                        "idempotent ExecutionRequest disappeared during submission"
                    )
                self._validate_request_intent(
                    concurrent,
                    record.flow_run_id,
                    operation,
                    payload,
                    target_kind=ExecutionTargetKind.FLOW_RUN,
                )
                return (
                    await self.flows._locked_record(connection, record.flow_run_id),
                    concurrent,
                )
            inserted = (
                await connection.execute(
                    postgresql_insert(self.tables.flow_runs)
                    .values(**_flow_run_columns(queued))
                    .on_conflict_do_nothing(
                        index_elements=[self.tables.flow_runs.c.flow_run_id]
                    )
                    .returning(self.tables.flow_runs.c.flow_run_id)
                )
            ).scalar_one_or_none()
            if inserted is None:
                raise FlowRunAlreadyExistsError(
                    f"Flow Run '{record.flow_run_id}' already exists"
                )
        return queued, request

    async def enqueue_flow_continuation(
        self,
        flow_run_id: str,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, ExecutionRequest]:
        if operation not in {ExecutionOperation.RESUME, ExecutionOperation.CONFIRM}:
            raise ValueError("Flow continuation operation must be resume or confirm")
        async with self.engine.begin() as connection:
            existing = await self._request_by_key(connection, idempotency_key)
            if existing is not None:
                self._validate_request_intent(
                    existing,
                    flow_run_id,
                    operation,
                    payload,
                    target_kind=ExecutionTargetKind.FLOW_RUN,
                )
                return (
                    await self.flows._locked_record(connection, flow_run_id),
                    existing,
                )
            run = await self.flows._locked_record(connection, flow_run_id)
            if run.status is not FlowStatus.WAITING:
                raise FlowTransitionError(
                    f"Flow Run '{flow_run_id}' cannot continue from "
                    f"status '{run.status.value}'"
                )
            now = await _database_now(connection)
            request = ExecutionRequest(
                target_kind=ExecutionTargetKind.FLOW_RUN,
                target_id=flow_run_id,
                operation=operation,
                payload=_finite_json_mapping(payload),
                runtime_route=run.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=run.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = FlowRunRecord.model_validate(
                {
                    **run.model_dump(mode="json"),
                    "status": FlowStatus.QUEUED,
                    "revision": run.revision + 1,
                    "current_request_id": request.request_id,
                    "updated_at": now,
                }
            )
            await self.flows._write_record(connection, queued)
            await connection.execute(
                postgresql_insert(self.tables.execution_requests).values(
                    **_execution_request_columns(request)
                )
            )
        return queued, request

    async def claim_next(
        self,
        *,
        owner_id: str,
        runtime_route: str,
        runtime_build_id: str,
        lease_seconds: float,
        target_kinds: Collection[ExecutionTargetKind] = (
            ExecutionTargetKind.AGENT_RUN,
        ),
    ) -> ClaimedExecution | None:
        _validate_run_lease(owner_id, lease_seconds)
        supported = frozenset(target_kinds)
        if not supported:
            raise ValueError("Execution Worker must support at least one target kind")
        async with self.engine.begin() as connection:
            statement = (
                select(self.tables.execution_requests.c.record)
                .where(
                    self.tables.execution_requests.c.target_kind.in_(
                        tuple(item.value for item in supported)
                    ),
                    self.tables.execution_requests.c.status
                    == ExecutionRequestStatus.PENDING.value,
                    self.tables.execution_requests.c.runtime_route == runtime_route,
                    self.tables.execution_requests.c.available_at <= func.clock_timestamp(),
                )
                .order_by(self.tables.execution_requests.c.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            payload = (await connection.execute(statement)).scalar_one_or_none()
            if payload is None:
                return None
            request = ExecutionRequest.model_validate(payload)
            target = await self._locked_target(
                connection,
                request.target_kind,
                request.target_id,
            )
            now = await _database_now(connection)
            if (
                target.status.value != "queued"
                or target.current_request_id != request.request_id
                or target.revision != request.expected_target_revision
            ):
                discarded = request.model_copy(
                    update={
                        "status": ExecutionRequestStatus.DISCARDED,
                        "updated_at": now,
                    }
                )
                await self._write_request(connection, discarded)
                return None
            attempt_number = int(
                (
                    await connection.execute(
                        select(
                            func.coalesce(
                                func.max(self.tables.execution_attempts.c.attempt_number),
                                0,
                            )
                        ).where(
                            self.tables.execution_attempts.c.target_kind
                            == request.target_kind.value,
                            self.tables.execution_attempts.c.target_id
                            == request.target_id,
                        )
                    )
                ).scalar_one()
            ) + 1
            token = str(uuid4())
            expires_at = now + timedelta(seconds=lease_seconds)
            attempt = ExecutionAttempt(
                target_kind=request.target_kind,
                target_id=request.target_id,
                request_id=request.request_id,
                attempt_number=attempt_number,
                owner_id=owner_id,
                fencing_token=token,
                runtime_build_id=runtime_build_id,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=expires_at,
            )
            claimed_request = request.model_copy(
                update={
                    "status": ExecutionRequestStatus.CLAIMED,
                    "claimed_attempt_id": attempt.attempt_id,
                    "updated_at": now,
                }
            )
            running = self._updated_target(
                target,
                status="running",
                now=now,
                updates={
                    "current_attempt_id": attempt.attempt_id,
                    "recovery_reason_code": None,
                    "recovery_detected_at": None,
                    "error": None,
                    "pending_input": None,
                    "waiting_child_run_id": None,
                },
            )
            await connection.execute(
                postgresql_insert(self.tables.execution_attempts).values(
                    **_execution_attempt_columns(attempt)
                )
            )
            await self._write_request(connection, claimed_request)
            await self._write_target(connection, running)
            await _append_event_in_transaction(
                connection,
                self.tables,
                request.target_id,
                "execution.attempt.claimed",
                {
                    "execution_attempt_id": attempt.attempt_id,
                    "request_id": request.request_id,
                    "attempt_number": attempt.attempt_number,
                    "owner_id": attempt.owner_id,
                    "runtime_build_id": attempt.runtime_build_id,
                },
                turn=None,
            )
        return ClaimedExecution(
            request=claimed_request,
            attempt=attempt,
            lease=ExecutionLease(
                attempt_id=attempt.attempt_id,
                request_id=request.request_id,
                target_id=request.target_id,
                owner_id=owner_id,
                fencing_token=token,
                expires_at=expires_at,
            ),
            target=running,
        )

    async def mark_started(
        self,
        lease: ExecutionLease,
        *,
        child_pid: int | None = None,
    ) -> ExecutionAttempt:
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            target = await self._locked_target(
                connection,
                attempt.target_kind,
                lease.target_id,
            )
            _validate_current_attempt_target(attempt, target)
            started = attempt.model_copy(
                update={
                    "status": ExecutionAttemptStatus.RUNNING,
                    "child_pid": child_pid,
                    "started_at": now,
                    "revision": attempt.revision + 1,
                }
            )
            await self._write_attempt(connection, started)
        return started

    async def assert_live(self, lease: ExecutionLease) -> None:
        async with self.engine.connect() as connection:
            statement = select(self.tables.execution_attempts.c.record).where(
                self.tables.execution_attempts.c.attempt_id == lease.attempt_id
            )
            payload = (await connection.execute(statement)).scalar_one_or_none()
            if payload is None:
                raise ExecutionAttemptNotFoundError(
                    f"ExecutionAttempt '{lease.attempt_id}' was not found"
                )
            attempt = ExecutionAttempt.model_validate(payload)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            target = await self._locked_target(
                connection,
                attempt.target_kind,
                lease.target_id,
            )
            _validate_current_attempt_target(attempt, target)

    async def renew(
        self,
        lease: ExecutionLease,
        *,
        lease_seconds: float,
    ) -> AttemptHeartbeat:
        _validate_run_lease(lease.owner_id, lease_seconds)
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            target = await self._locked_target(
                connection,
                attempt.target_kind,
                lease.target_id,
            )
            _validate_current_attempt_target(attempt, target)
            expires_at = now + timedelta(seconds=lease_seconds)
            renewed = attempt.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": expires_at,
                    "revision": attempt.revision + 1,
                }
            )
            await self._write_attempt(connection, renewed)
        return AttemptHeartbeat(
            lease=lease.model_copy(update={"expires_at": expires_at}),
            cancel_requested=target.cancel_requested,
        )

    async def settle(
        self,
        lease: ExecutionLease,
        *,
        status: RunStatus | FlowStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: Mapping[str, Any] | None = None,
        checkpoint_revision: int | None = None,
        waiting_child_run_id: str | None = None,
    ) -> AgentRunRecord | FlowRunRecord:
        attempt_status = {
            "waiting": ExecutionAttemptStatus.SUSPENDED,
            "succeeded": ExecutionAttemptStatus.SUCCEEDED,
            "failed": ExecutionAttemptStatus.FAILED,
            "cancelled": ExecutionAttemptStatus.CANCELLED,
        }.get(status.value)
        if attempt_status is None:
            raise RunTransitionError(
                f"Attempt cannot settle logical target as '{status.value}'"
            )
        if status.value == "waiting" and checkpoint_revision is None:
            raise RunTransitionError("WAITING settlement requires checkpoint_revision")
        if status.value != "waiting" and checkpoint_revision is not None:
            raise RunTransitionError("only WAITING settlement may retain a checkpoint")
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            target = await self._locked_target(
                connection,
                attempt.target_kind,
                lease.target_id,
            )
            if (
                target.status.value != "running"
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for its target"
                )
            updates: dict[str, Any] = {
                    "current_attempt_id": None,
                    "checkpoint_revision": checkpoint_revision,
                    "pending_input": (
                        _finite_json_mapping(pending_input)
                        if pending_input is not None
                        else None
                    ),
                    "error": error,
            }
            if attempt.target_kind is ExecutionTargetKind.AGENT_RUN:
                updates.update(output=_finite_json_value(output), text=text)
            else:
                updates.update(
                    output=text if text is not None else _postgres_flow_output(output),
                    waiting_child_run_id=waiting_child_run_id,
                )
                if status.value != "waiting":
                    updates["checkpoint_stage"] = None
            settled = self._updated_target(
                target,
                status=status.value,
                now=now,
                updates=updates,
            )
            ended = attempt.model_copy(
                update={
                    "status": attempt_status,
                    "ended_at": now,
                    "revision": attempt.revision + 1,
                }
            )
            request = await self._locked_request(connection, attempt.request_id)
            completed = request.model_copy(
                update={
                    "status": ExecutionRequestStatus.COMPLETED,
                    "updated_at": now,
                }
            )
            if status.value in {"failed", "cancelled"}:
                await _mark_open_side_effects_unknown_in_transaction(
                    connection,
                    self.tables,
                    attempt.attempt_id,
                    now=now,
                )
            await self._write_attempt(connection, ended)
            await self._write_request(connection, completed)
            await self._write_target(connection, settled)
            await _append_event_in_transaction(
                connection,
                self.tables,
                request.target_id,
                _distributed_terminal_event_kind(request.target_kind, status.value),
                _distributed_terminal_event_payload(
                    attempt.attempt_id,
                    target_kind=request.target_kind,
                    status=status.value,
                    output=output,
                    text=text,
                    error=error,
                    checkpoint_revision=checkpoint_revision,
                    pending_input=pending_input,
                    waiting_child_run_id=waiting_child_run_id,
                ),
                turn=None,
            )
            if (
                request.target_kind is ExecutionTargetKind.FLOW_RUN
                and status.value in {"succeeded", "failed", "cancelled"}
            ):
                await connection.execute(
                    delete(self.tables.flow_checkpoints).where(
                        self.tables.flow_checkpoints.c.flow_run_id
                        == request.target_id
                    )
                )
        return settled

    async def interrupt(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
    ) -> AgentRunRecord | FlowRunRecord:
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now, require_unexpired=False)
            return await self._recover_attempt(
                connection,
                attempt,
                status=ExecutionAttemptStatus.INTERRUPTED,
                reason_code=reason_code,
                now=now,
            )

    async def block(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
        error: str,
    ) -> AgentRunRecord | FlowRunRecord:
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            target = await self._locked_target(
                connection,
                attempt.target_kind,
                lease.target_id,
            )
            if (
                target.status.value != "running"
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for its target"
                )
            ended = attempt.model_copy(
                update={
                    "status": ExecutionAttemptStatus.FAILED,
                    "ended_at": now,
                    "reason_code": reason_code,
                    "revision": attempt.revision + 1,
                }
            )
            request = await self._locked_request(connection, attempt.request_id)
            blocked_request = request.model_copy(
                update={"status": ExecutionRequestStatus.BLOCKED, "updated_at": now}
            )
            blocked = self._updated_target(
                target,
                status="blocked",
                now=now,
                updates={
                    "current_attempt_id": None,
                    "recovery_reason_code": reason_code,
                    "recovery_detected_at": now,
                    "error": error,
                },
            )
            await _mark_open_side_effects_unknown_in_transaction(
                connection,
                self.tables,
                attempt.attempt_id,
                now=now,
            )
            await self._write_attempt(connection, ended)
            await self._write_request(connection, blocked_request)
            await self._write_target(connection, blocked)
            await _append_event_in_transaction(
                connection,
                self.tables,
                request.target_id,
                "execution.request.blocked",
                {
                    "execution_attempt_id": attempt.attempt_id,
                    "request_id": request.request_id,
                    "reason_code": reason_code,
                },
                turn=None,
            )
            await _append_event_in_transaction(
                connection,
                self.tables,
                request.target_id,
                (
                    "run.blocked"
                    if request.target_kind is ExecutionTargetKind.AGENT_RUN
                    else "flow.blocked"
                ),
                {
                    "execution_attempt_id": attempt.attempt_id,
                    "reason_code": reason_code,
                    "error": error,
                },
                turn=None,
            )
        return blocked

    async def reconcile_expired(self) -> tuple[RecoveryTransition, ...]:
        active = tuple(status.value for status in _ACTIVE_EXECUTION_ATTEMPT_STATUSES)
        async with self.engine.begin() as connection:
            statement = (
                select(self.tables.execution_attempts.c.record)
                .where(
                    self.tables.execution_attempts.c.status.in_(active),
                    self.tables.execution_attempts.c.lease_expires_at
                    <= func.clock_timestamp(),
                )
                .order_by(self.tables.execution_attempts.c.lease_expires_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            payloads = (await connection.execute(statement)).scalars().all()
            now = await _database_now(connection)
            transitions: list[RecoveryTransition] = []
            for payload in payloads:
                attempt = ExecutionAttempt.model_validate(payload)
                target = await self._recover_attempt(
                    connection,
                    attempt,
                    status=ExecutionAttemptStatus.LEASE_EXPIRED,
                    reason_code="owner_lease_expired",
                    now=now,
                )
                refreshed = await self._locked_attempt(connection, attempt.attempt_id)
                transitions.append(
                    RecoveryTransition(target=target, attempt=refreshed)
                )
        return tuple(transitions)

    async def latest_attempt(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> ExecutionAttempt | None:
        statement = (
            select(self.tables.execution_attempts.c.record)
            .where(
                self.tables.execution_attempts.c.target_kind
                == target_kind.value,
                self.tables.execution_attempts.c.target_id == target_id,
            )
            .order_by(desc(self.tables.execution_attempts.c.attempt_number))
            .limit(1)
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        return None if payload is None else ExecutionAttempt.model_validate(payload)

    async def record_event_evidence(
        self,
        lease: ExecutionLease,
        kind: str,
        payload: Mapping[str, Any],
        *,
        turn: int | None = None,
    ) -> None:
        normalized = _finite_json_mapping(payload)
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            target = await self._locked_target(
                connection,
                attempt.target_kind,
                attempt.target_id,
            )
            _validate_current_attempt_target(attempt, target)
            await _record_event_evidence_in_transaction(
                connection,
                self.tables,
                attempt,
                kind,
                normalized,
                turn=turn,
                now=now,
            )

    async def recovery_evidence(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> RecoveryEvidenceSnapshot:
        async with self.engine.begin() as connection:
            target = await self._locked_target(connection, target_kind, target_id)
            return await self._recovery_evidence_locked(connection, target)

    async def request_cancel(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> AgentRunRecord | FlowRunRecord:
        async with self.engine.begin() as connection:
            target = await self._locked_target(connection, target_kind, target_id)
            now = await _database_now(connection)
            if target.status.value == "queued":
                if target.current_request_id is not None:
                    request = await self._locked_request(
                        connection,
                        target.current_request_id,
                    )
                    cancelled_request = request.model_copy(
                        update={
                            "status": ExecutionRequestStatus.CANCELLED,
                            "updated_at": now,
                        }
                    )
                    await self._write_request(connection, cancelled_request)
                cancelled = self._updated_target(
                    target,
                    status="cancelled",
                    now=now,
                    updates={
                        "cancel_requested": True,
                        "checkpoint_revision": None,
                        "checkpoint_stage": None,
                        "waiting_child_run_id": None,
                        "pending_input": None,
                        "error": "execution cancellation requested",
                    },
                )
                await self._write_target(connection, cancelled)
                await _append_event_in_transaction(
                    connection,
                    self.tables,
                    target_id,
                    (
                        "run.cancelled"
                        if target_kind is ExecutionTargetKind.AGENT_RUN
                        else "flow.cancelled"
                    ),
                    {"error": cancelled.error, "source": "execution_store"},
                    turn=None,
                )
                if target_kind is ExecutionTargetKind.FLOW_RUN:
                    await self._delete_flow_checkpoint(connection, target_id)
                return cancelled
            if target.status.value == "running":
                if target.cancel_requested:
                    return target
                requested = self._updated_target(
                    target,
                    status="running",
                    now=now,
                    updates={
                        "cancel_requested": True,
                    },
                )
                await self._write_target(connection, requested)
                return requested
            if target.status.value in {"waiting", "recovery_required", "blocked"}:
                cancelled = self._updated_target(
                    target,
                    status="cancelled",
                    now=now,
                    updates={
                        "cancel_requested": True,
                        "checkpoint_revision": None,
                        "checkpoint_stage": None,
                        "waiting_child_run_id": None,
                        "pending_input": None,
                        "error": "execution cancellation requested",
                    },
                )
                await self._write_target(connection, cancelled)
                await _append_event_in_transaction(
                    connection,
                    self.tables,
                    target_id,
                    (
                        "run.cancelled"
                        if target_kind is ExecutionTargetKind.AGENT_RUN
                        else "flow.cancelled"
                    ),
                    {"error": cancelled.error, "source": "execution_store"},
                    turn=None,
                )
                if target_kind is ExecutionTargetKind.FLOW_RUN:
                    await self._delete_flow_checkpoint(connection, target_id)
                return cancelled
            if target.status.value == "cancelled":
                return target
            raise FlowTransitionError(
                f"execution target '{target_id}' already reached "
                f"status '{target.status.value}'"
            )

    async def decide_recovery(
        self,
        target_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> tuple[AgentRunRecord | FlowRunRecord, RecoveryDecisionRecord]:
        async with self.engine.begin() as connection:
            existing = await self._decision_by_key(connection, idempotency_key)
            if existing is not None:
                _validate_recovery_intent(
                    existing,
                    run_id=target_id,
                    decision=decision,
                    strategy=strategy,
                    subject_id=subject_id,
                    reason_code=reason_code,
                    expected_revision=expected_revision,
                )
                if existing.target_kind is not target_kind:
                    raise ExecutionRequestConflictError(
                        "recovery idempotency key targets another execution kind"
                    )
                return (
                    await self._locked_target(connection, target_kind, target_id),
                    existing,
                )
            target = await self._locked_target(connection, target_kind, target_id)
            concurrent = await self._decision_by_key(connection, idempotency_key)
            if concurrent is not None:
                _validate_recovery_intent(
                    concurrent,
                    run_id=target_id,
                    decision=decision,
                    strategy=strategy,
                    subject_id=subject_id,
                    reason_code=reason_code,
                    expected_revision=expected_revision,
                )
                if concurrent.target_kind is not target_kind:
                    raise ExecutionRequestConflictError(
                        "recovery idempotency key targets another execution kind"
                    )
                return target, concurrent
            if target.status.value not in {"recovery_required", "blocked"}:
                raise FlowTransitionError(
                    f"execution target '{target_id}' does not require an "
                    "Operator recovery decision"
                )
            if target.revision != expected_revision:
                raise FlowTransitionError(
                    f"execution target '{target_id}' revision changed from "
                    f"{expected_revision} to {target.revision}"
                )
            if decision is RecoveryDecision.RETRY and strategy is None:
                raise ValueError("retry recovery decision requires a strategy")
            if decision is not RecoveryDecision.RETRY and strategy is not None:
                raise ValueError("only retry recovery decision accepts a strategy")
            if (
                target_kind is ExecutionTargetKind.AGENT_RUN
                and strategy is RecoveryStrategy.FROM_STAGE
            ):
                raise ValueError("Agent Run recovery does not support from_stage")
            if (
                strategy in {
                    RecoveryStrategy.FROM_CHECKPOINT,
                    RecoveryStrategy.FROM_STAGE,
                }
                and target.checkpoint_revision is None
            ):
                raise FlowTransitionError(
                    "execution target has no checkpoint to recover from"
                )
            if (
                strategy is RecoveryStrategy.FROM_STAGE
                and isinstance(target, FlowRunRecord)
                and target.checkpoint_stage is None
            ):
                raise FlowTransitionError("Flow Run has no durable Stage checkpoint")
            now = await _database_now(connection)
            latest = await self._latest_attempt_locked(
                connection,
                target_id,
                target_kind=target_kind,
            )
            created_request: ExecutionRequest | None = None
            if decision is RecoveryDecision.RETRY:
                assert strategy is not None
                evidence = await self._recovery_evidence_locked(connection, target)
                blockers = evidence.blockers_for(strategy)
                if blockers:
                    raise RecoveryGateBlockedError(
                        _recovery_gate_error(strategy, blockers)
                    )
                source_operation = (
                    ExecutionOperation.EXECUTE
                    if strategy is RecoveryStrategy.RESTART
                    else None
                )
                original_statement = (
                    select(self.tables.execution_requests.c.record)
                    .where(
                        self.tables.execution_requests.c.target_kind
                        == target_kind.value,
                        self.tables.execution_requests.c.target_id == target_id,
                        *(
                            (
                                self.tables.execution_requests.c.operation
                                == source_operation.value,
                            )
                            if source_operation is not None
                            else (
                                self.tables.execution_requests.c.request_id
                                == target.current_request_id,
                            )
                        ),
                    )
                    .order_by(self.tables.execution_requests.c.created_at)
                    .limit(1)
                )
                original_payload = (
                    await connection.execute(original_statement)
                ).scalar_one_or_none()
                if original_payload is None:
                    raise ExecutionRequestConflictError(
                        f"original execute request for target '{target_id}' was not found"
                    )
                original = ExecutionRequest.model_validate(original_payload)
                request_payload = dict(original.payload)
                assert strategy is not None
                request_payload["recovery_strategy"] = strategy.value
                request_payload["recovery_operation"] = original.operation.value
                created_request = ExecutionRequest(
                    target_kind=target_kind,
                    target_id=target_id,
                    operation=ExecutionOperation.RECOVER,
                    payload=request_payload,
                    runtime_route=target.runtime_route,
                    idempotency_key=_derived_idempotency_key("recovery", idempotency_key),
                    expected_target_revision=target.revision + 1,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
                updated = self._updated_target(
                    target,
                    status="queued",
                    now=now,
                    updates={
                        "current_request_id": created_request.request_id,
                        "current_attempt_id": None,
                        "cancel_requested": False,
                        **(
                            {
                                "checkpoint_revision": None,
                                "pending_input": None,
                            }
                            if strategy is RecoveryStrategy.RESTART
                            else {}
                        ),
                        **(
                            {
                                "checkpoint_stage": None,
                                "plan_revision": None,
                                "waiting_child_run_id": None,
                            }
                            if target_kind is ExecutionTargetKind.FLOW_RUN
                            and strategy is RecoveryStrategy.RESTART
                            else {}
                        ),
                    },
                )
                if (
                    target_kind is ExecutionTargetKind.FLOW_RUN
                    and strategy is RecoveryStrategy.RESTART
                ):
                    await self._delete_flow_checkpoint(connection, target_id)
                await connection.execute(
                    postgresql_insert(self.tables.execution_requests).values(
                        **_execution_request_columns(created_request)
                    )
                )
            else:
                target_status = (
                    "cancelled"
                    if decision is RecoveryDecision.CANCEL
                    else "failed"
                )
                updated = self._updated_target(
                    target,
                    status=target_status,
                    now=now,
                    updates={
                        "current_attempt_id": None,
                        "cancel_requested": decision is RecoveryDecision.CANCEL,
                        "checkpoint_revision": None,
                        "checkpoint_stage": None,
                        "waiting_child_run_id": None,
                        "pending_input": None,
                        "error": reason_code,
                    },
                )
            record = RecoveryDecisionRecord(
                target_kind=target_kind,
                target_id=target_id,
                source_attempt_id=latest.attempt_id if latest is not None else None,
                decision=decision,
                strategy=strategy,
                subject_id=subject_id,
                reason_code=reason_code,
                expected_target_revision=expected_revision,
                idempotency_key=idempotency_key,
                created_request_id=(
                    created_request.request_id if created_request is not None else None
                ),
                created_at=now,
            )
            await self._write_target(connection, updated)
            await connection.execute(
                postgresql_insert(self.tables.recovery_decisions).values(
                    **_recovery_decision_columns(record)
                )
            )
            if decision is not RecoveryDecision.RETRY:
                await _append_event_in_transaction(
                    connection,
                    self.tables,
                    target_id,
                    _distributed_terminal_event_kind(
                        target_kind,
                        updated.status.value,
                    ),
                    _distributed_terminal_event_payload(
                        latest.attempt_id if latest is not None else None,
                        target_kind=target_kind,
                        status=updated.status.value,
                        output=None,
                        text="",
                        error=updated.error,
                        checkpoint_revision=None,
                    ),
                    turn=None,
                )
                if target_kind is ExecutionTargetKind.FLOW_RUN:
                    await self._delete_flow_checkpoint(connection, target_id)
        return updated, record

    async def save_flow_checkpoint(
        self,
        lease: ExecutionLease,
        checkpoint: FlowCheckpoint,
    ) -> FlowCheckpoint:
        async with self.engine.begin() as connection:
            attempt = await self._locked_attempt(connection, lease.attempt_id)
            now = await _database_now(connection)
            self._validate_owned_attempt(attempt, lease, now)
            if attempt.target_kind is not ExecutionTargetKind.FLOW_RUN:
                raise ExecutionLeaseLostError(
                    "an Agent Attempt cannot write a Flow checkpoint"
                )
            if checkpoint.flow_run_id != attempt.target_id:
                raise ValueError("Flow checkpoint targets another Flow Run")
            target = await self.flows._locked_record(connection, attempt.target_id)
            if (
                target.status is not FlowStatus.RUNNING
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for the Flow Run"
                )
            if (
                target.flow_id != checkpoint.flow_id
                or target.flow_kind is not checkpoint.flow_kind
                or target.definition_fingerprint != checkpoint.definition_fingerprint
            ):
                raise FlowCheckpointConflictError(
                    "Flow checkpoint does not match its pinned Flow definition"
                )
            statement = (
                select(self.tables.flow_checkpoints.c.payload)
                .where(
                    self.tables.flow_checkpoints.c.flow_run_id
                    == checkpoint.flow_run_id
                )
                .with_for_update()
            )
            payload = (await connection.execute(statement)).scalar_one_or_none()
            current = (
                None if payload is None else FlowCheckpoint.model_validate(payload)
            )
            current_revision = current.revision if current is not None else None
            if current_revision != target.checkpoint_revision:
                raise FlowCheckpointConflictError(
                    f"Flow checkpoint revision changed from "
                    f"{target.checkpoint_revision} to {current_revision}"
                )
            expected_next = 1 if current is None else current.revision + 1
            if checkpoint.revision != expected_next:
                raise FlowCheckpointConflictError(
                    f"Flow checkpoint revision must advance to {expected_next}"
                )
            normalized = FlowCheckpoint.model_validate(
                flow_checkpoint_json(checkpoint)
            )
            receipt_updates = await _checkpoint_receipt_updates_in_transaction(
                connection,
                self.tables,
                attempt,
                normalized,
                now=now,
            )
            if current is None:
                await connection.execute(
                    postgresql_insert(self.tables.flow_checkpoints).values(
                        **_flow_checkpoint_columns(normalized)
                    )
                )
            else:
                await connection.execute(
                    update(self.tables.flow_checkpoints)
                    .where(
                        self.tables.flow_checkpoints.c.flow_run_id
                        == checkpoint.flow_run_id
                    )
                    .values(**_flow_checkpoint_columns(normalized))
                )
            for receipt in receipt_updates:
                await _write_artifact_receipt(connection, self.tables, receipt)
            await _mark_attempt_evidence_checkpointed_in_transaction(
                connection,
                self.tables,
                attempt.attempt_id,
                checkpoint_revision=normalized.revision,
                now=now,
            )
            updated = self._updated_target(
                target,
                status="running",
                now=now,
                updates={
                    "checkpoint_revision": normalized.revision,
                    "checkpoint_stage": normalized.stage,
                    "plan_revision": normalized.plan_revision,
                    "waiting_child_run_id": normalized.waiting_child_run_id,
                },
            )
            assert isinstance(updated, FlowRunRecord)
            await self.flows._write_record(connection, updated)
        return normalized

    async def load_flow_checkpoint(self, flow_run_id: str) -> FlowCheckpoint:
        return await self.flows.load(flow_run_id)

    async def _recover_attempt(
        self,
        connection: Any,
        attempt: ExecutionAttempt,
        *,
        status: ExecutionAttemptStatus,
        reason_code: str,
        now: datetime,
    ) -> AgentRunRecord | FlowRunRecord:
        await _mark_open_side_effects_unknown_in_transaction(
            connection,
            self.tables,
            attempt.attempt_id,
            now=now,
        )
        ended = attempt.model_copy(
            update={
                "status": status,
                "ended_at": now,
                "reason_code": reason_code,
                "revision": attempt.revision + 1,
            }
        )
        await self._write_attempt(connection, ended)
        request = await self._locked_request(connection, attempt.request_id)
        interrupted = request.model_copy(
            update={
                "status": ExecutionRequestStatus.INTERRUPTED,
                "updated_at": now,
            }
        )
        await self._write_request(connection, interrupted)
        target = await self._locked_target(
            connection,
            attempt.target_kind,
            attempt.target_id,
        )
        if (
            target.status.value != "running"
            or target.current_attempt_id != attempt.attempt_id
        ):
            return target
        recovery = self._updated_target(
            target,
            status="recovery_required",
            now=now,
            updates={
                "current_attempt_id": None,
                "recovery_reason_code": reason_code,
                "recovery_detected_at": now,
                "error": "Execution ownership was lost; Operator decision is required",
            },
        )
        await self._write_target(connection, recovery)
        await _append_event_in_transaction(
            connection,
            self.tables,
            attempt.target_id,
            (
                "execution.attempt.lease_expired"
                if status is ExecutionAttemptStatus.LEASE_EXPIRED
                else "execution.attempt.interrupted"
            ),
            {
                "execution_attempt_id": attempt.attempt_id,
                "owner_id": attempt.owner_id,
                "runtime_build_id": attempt.runtime_build_id,
                "reason_code": reason_code,
            },
            turn=None,
        )
        await _append_event_in_transaction(
            connection,
            self.tables,
            attempt.target_id,
            (
                "run.recovery_required"
                if attempt.target_kind is ExecutionTargetKind.AGENT_RUN
                else "flow.recovery_required"
            ),
            {
                "execution_attempt_id": attempt.attempt_id,
                "reason_code": reason_code,
                "revision": recovery.revision,
            },
            turn=None,
        )
        return recovery

    async def _locked_request(self, connection: Any, request_id: str) -> ExecutionRequest:
        statement = (
            select(self.tables.execution_requests.c.record)
            .where(self.tables.execution_requests.c.request_id == request_id)
            .with_for_update()
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise ExecutionRequestConflictError(
                f"ExecutionRequest '{request_id}' was not found"
            )
        return ExecutionRequest.model_validate(payload)

    async def _locked_attempt(self, connection: Any, attempt_id: str) -> ExecutionAttempt:
        statement = (
            select(self.tables.execution_attempts.c.record)
            .where(self.tables.execution_attempts.c.attempt_id == attempt_id)
            .with_for_update()
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise ExecutionAttemptNotFoundError(
                f"ExecutionAttempt '{attempt_id}' was not found"
            )
        return ExecutionAttempt.model_validate(payload)

    async def _request_by_key(
        self,
        connection: Any,
        idempotency_key: str,
    ) -> ExecutionRequest | None:
        statement = select(self.tables.execution_requests.c.record).where(
            self.tables.execution_requests.c.idempotency_key == idempotency_key
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        return None if payload is None else ExecutionRequest.model_validate(payload)

    async def _decision_by_key(
        self,
        connection: Any,
        idempotency_key: str,
    ) -> RecoveryDecisionRecord | None:
        statement = select(self.tables.recovery_decisions.c.record).where(
            self.tables.recovery_decisions.c.idempotency_key == idempotency_key
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        return None if payload is None else RecoveryDecisionRecord.model_validate(payload)

    async def _latest_attempt_locked(
        self,
        connection: Any,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind,
    ) -> ExecutionAttempt | None:
        statement = (
            select(self.tables.execution_attempts.c.record)
            .where(
                self.tables.execution_attempts.c.target_kind == target_kind.value,
                self.tables.execution_attempts.c.target_id == target_id,
            )
            .order_by(desc(self.tables.execution_attempts.c.attempt_number))
            .limit(1)
        )
        payload = (await connection.execute(statement)).scalar_one_or_none()
        return None if payload is None else ExecutionAttempt.model_validate(payload)

    async def _recovery_evidence_locked(
        self,
        connection: Any,
        target: AgentRunRecord | FlowRunRecord,
    ) -> RecoveryEvidenceSnapshot:
        target_kind = (
            ExecutionTargetKind.AGENT_RUN
            if isinstance(target, AgentRunRecord)
            else ExecutionTargetKind.FLOW_RUN
        )
        target_id = target.run_id if isinstance(target, AgentRunRecord) else target.flow_run_id
        receipt_payloads = (
            await connection.execute(
                select(self.tables.artifact_receipts.c.record)
                .where(
                    self.tables.artifact_receipts.c.target_kind == target_kind.value,
                    self.tables.artifact_receipts.c.target_id == target_id,
                )
                .order_by(self.tables.artifact_receipts.c.created_at)
            )
        ).scalars().all()
        side_effect_payloads = (
            await connection.execute(
                select(self.tables.side_effect_evidence.c.record)
                .where(
                    self.tables.side_effect_evidence.c.target_kind == target_kind.value,
                    self.tables.side_effect_evidence.c.target_id == target_id,
                )
                .order_by(self.tables.side_effect_evidence.c.created_at)
            )
        ).scalars().all()
        receipts = tuple(ArtifactReceipt.model_validate(item) for item in receipt_payloads)
        side_effects = tuple(
            SideEffectEvidence.model_validate(item) for item in side_effect_payloads
        )
        checkpoint: FlowCheckpoint | None = None
        checkpoint_error: str | None = None
        if isinstance(target, FlowRunRecord) and target.checkpoint_revision is not None:
            checkpoint_payload = (
                await connection.execute(
                    select(self.tables.flow_checkpoints.c.payload).where(
                        self.tables.flow_checkpoints.c.flow_run_id == target_id
                    )
                )
            ).scalar_one_or_none()
            if checkpoint_payload is None:
                checkpoint_error = "durable Flow checkpoint was not found"
            else:
                checkpoint = FlowCheckpoint.model_validate(checkpoint_payload)
                if checkpoint.revision != target.checkpoint_revision:
                    checkpoint_error = (
                        "Flow checkpoint revision does not match the logical Flow Run"
                    )
        return RecoveryEvidenceSnapshot(
            target_kind=target_kind,
            target_id=target_id,
            checkpoint_revision=target.checkpoint_revision,
            checkpoint_stage=(
                target.checkpoint_stage if isinstance(target, FlowRunRecord) else None
            ),
            artifact_receipts=receipts,
            side_effect_evidence=side_effects,
            strategy_blockers=_recovery_strategy_blockers(
                target,
                receipts=receipts,
                side_effects=side_effects,
                checkpoint=checkpoint,
                checkpoint_error=checkpoint_error,
            ),
        )

    def _validate_owned_attempt(
        self,
        attempt: ExecutionAttempt,
        lease: ExecutionLease,
        now: datetime,
        *,
        require_unexpired: bool = True,
    ) -> None:
        if (
            attempt.status not in _ACTIVE_EXECUTION_ATTEMPT_STATUSES
            or attempt.owner_id != lease.owner_id
            or attempt.fencing_token != lease.fencing_token
            or attempt.request_id != lease.request_id
            or attempt.target_id != lease.target_id
        ):
            raise ExecutionLeaseLostError(
                f"ExecutionAttempt lease '{lease.attempt_id}' is no longer owned"
            )
        if require_unexpired and attempt.lease_expires_at <= now:
            raise ExecutionLeaseLostError(
                f"ExecutionAttempt lease '{lease.attempt_id}' has expired"
            )

    @staticmethod
    def _validate_request_intent(
        request: ExecutionRequest,
        target_id: str,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> None:
        if (
            request.target_kind is not target_kind
            or request.target_id != target_id
            or request.operation is not operation
            or request.payload != _finite_json_mapping(payload)
        ):
            raise ExecutionRequestConflictError("idempotency key has different intent")

    async def _write_run(self, connection: Any, record: AgentRunRecord) -> None:
        await connection.execute(
            update(self.tables.runs)
            .where(self.tables.runs.c.run_id == record.run_id)
            .values(**_run_columns(record))
        )

    async def _locked_target(
        self,
        connection: Any,
        target_kind: ExecutionTargetKind,
        target_id: str,
    ) -> AgentRunRecord | FlowRunRecord:
        if target_kind is ExecutionTargetKind.AGENT_RUN:
            return await self.runs._locked_record(connection, target_id)
        return await self.flows._locked_record(connection, target_id)

    async def _write_target(
        self,
        connection: Any,
        record: AgentRunRecord | FlowRunRecord,
    ) -> None:
        if isinstance(record, AgentRunRecord):
            await self._write_run(connection, record)
        else:
            await self.flows._write_record(connection, record)

    @staticmethod
    def _updated_target(
        target: AgentRunRecord | FlowRunRecord,
        *,
        status: str,
        now: datetime,
        updates: Mapping[str, Any],
    ) -> AgentRunRecord | FlowRunRecord:
        values = {
            "status": status,
            "revision": target.revision + 1,
            "updated_at": now,
            **dict(updates),
        }
        if isinstance(target, AgentRunRecord):
            for name in ("checkpoint_stage", "plan_revision", "waiting_child_run_id"):
                values.pop(name, None)
            return AgentRunRecord.model_validate(
                {**target.model_dump(mode="json"), **values}
            )
        values.pop("text", None)
        return FlowRunRecord.model_validate(
            {**target.model_dump(mode="json"), **values}
        )

    async def _delete_flow_checkpoint(
        self,
        connection: Any,
        flow_run_id: str,
    ) -> None:
        await connection.execute(
            delete(self.tables.flow_checkpoints).where(
                self.tables.flow_checkpoints.c.flow_run_id == flow_run_id
            )
        )

    async def _write_request(self, connection: Any, record: ExecutionRequest) -> None:
        await connection.execute(
            update(self.tables.execution_requests)
            .where(self.tables.execution_requests.c.request_id == record.request_id)
            .values(**_execution_request_columns(record))
        )

    async def _write_attempt(self, connection: Any, record: ExecutionAttempt) -> None:
        await connection.execute(
            update(self.tables.execution_attempts)
            .where(self.tables.execution_attempts.c.attempt_id == record.attempt_id)
            .values(**_execution_attempt_columns(record))
        )


_ACTIVE_EXECUTION_ATTEMPT_STATUSES = frozenset(
    {
        ExecutionAttemptStatus.CLAIMED,
        ExecutionAttemptStatus.STARTING,
        ExecutionAttemptStatus.RUNNING,
        ExecutionAttemptStatus.STOPPING,
    }
)


async def _record_event_evidence_in_transaction(
    connection: Any,
    tables: PostgresEventTables,
    attempt: ExecutionAttempt,
    kind: str,
    payload: Mapping[str, Any],
    *,
    turn: int | None,
    now: datetime,
) -> None:
    if kind == "artifact.created":
        receipt = _artifact_receipt_from_event(attempt, payload, now=now)
        existing_payload = (
            await connection.execute(
                select(tables.artifact_receipts.c.record)
                .where(tables.artifact_receipts.c.artifact_id == receipt.artifact_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing_payload is not None:
            _validate_artifact_receipt_intent(
                ArtifactReceipt.model_validate(existing_payload),
                receipt,
            )
            return
        await connection.execute(
            postgresql_insert(tables.artifact_receipts).values(
                **_artifact_receipt_columns(receipt)
            )
        )
        return
    if kind == "tool.started":
        evidence = _side_effect_started_from_event(
            attempt,
            payload,
            turn=turn,
            now=now,
        )
        intent_payloads = (
            await connection.execute(
                select(tables.side_effect_evidence.c.record)
                .where(
                    tables.side_effect_evidence.c.target_kind
                    == attempt.target_kind.value,
                    tables.side_effect_evidence.c.target_id == attempt.target_id,
                    tables.side_effect_evidence.c.operation_id == evidence.operation_id,
                )
                .with_for_update()
            )
        ).scalars().all()
        for existing_payload in intent_payloads:
            _validate_side_effect_intent(
                SideEffectEvidence.model_validate(existing_payload),
                evidence,
            )
        if any(
            SideEffectEvidence.model_validate(item).evidence_id == evidence.evidence_id
            for item in intent_payloads
        ):
            return
        await connection.execute(
            postgresql_insert(tables.side_effect_evidence).values(
                **_side_effect_evidence_columns(evidence)
            )
        )
        return
    if kind in {"tool.waiting", "tool.completed"}:
        operation_id = payload.get("operation_id")
        if operation_id is None:
            return
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ExecutionRequestConflictError(
                "Tool evidence operation_id must be a non-blank string"
            )
        evidence_id = _side_effect_evidence_id(operation_id, attempt.attempt_id)
        existing_payload = (
            await connection.execute(
                select(tables.side_effect_evidence.c.record)
                .where(tables.side_effect_evidence.c.evidence_id == evidence_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing_payload is None:
            if kind != "tool.completed":
                raise ExecutionRequestConflictError(
                    f"Tool operation '{operation_id}' has no current started evidence"
                )
            resumable_payloads = (
                await connection.execute(
                    select(tables.side_effect_evidence.c.record)
                    .where(
                        tables.side_effect_evidence.c.target_kind
                        == attempt.target_kind.value,
                        tables.side_effect_evidence.c.target_id == attempt.target_id,
                        tables.side_effect_evidence.c.operation_id == operation_id,
                        tables.side_effect_evidence.c.checkpoint_revision.is_not(None),
                    )
                    .with_for_update()
                )
            ).scalars().all()
            existing = _resumable_side_effect_evidence(
                tuple(
                    SideEffectEvidence.model_validate(item)
                    for item in resumable_payloads
                ),
                attempt,
                operation_id,
            )
        else:
            existing = SideEffectEvidence.model_validate(existing_payload)
        status, result_status = _side_effect_completion_status(kind, payload)
        completed = existing.model_copy(
            update={
                "status": status,
                "result_status": result_status,
                "completed_at": (now if kind == "tool.completed" else None),
                "resolved_by_attempt_id": (
                    attempt.attempt_id
                    if existing.execution_attempt_id != attempt.attempt_id
                    else existing.resolved_by_attempt_id
                ),
                "resolution_fencing_token": (
                    attempt.fencing_token
                    if existing.execution_attempt_id != attempt.attempt_id
                    else existing.resolution_fencing_token
                ),
                "updated_at": now,
            }
        )
        await _write_side_effect_evidence(connection, tables, completed)
        return
    if kind in {"run.waiting", "execution.child.waiting"}:
        checkpoint_revision = payload.get("checkpoint_revision")
        if isinstance(checkpoint_revision, int) and not isinstance(
            checkpoint_revision, bool
        ):
            await _mark_attempt_evidence_checkpointed_in_transaction(
                connection,
                tables,
                attempt.attempt_id,
                checkpoint_revision=checkpoint_revision,
                now=now,
            )


async def _checkpoint_receipt_updates_in_transaction(
    connection: Any,
    tables: PostgresEventTables,
    attempt: ExecutionAttempt,
    checkpoint: FlowCheckpoint,
    *,
    now: datetime,
) -> tuple[ArtifactReceipt, ...]:
    artifacts = _checkpoint_artifacts(checkpoint)
    if {artifact.id for artifact in artifacts} != set(checkpoint.artifact_ids):
        raise FlowTransitionError(
            "Flow checkpoint Artifact ids do not match its verified Artifact metadata"
        )
    updates: list[ArtifactReceipt] = []
    for artifact in artifacts:
        if artifact.flow_run_id != attempt.target_id:
            raise FlowTransitionError(
                f"Artifact '{artifact.id}' does not belong to the Flow Run"
            )
        pending = _artifact_receipt_from_artifact(attempt, artifact, now=now)
        existing_payload = (
            await connection.execute(
                select(tables.artifact_receipts.c.record)
                .where(tables.artifact_receipts.c.artifact_id == artifact.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing_payload is not None:
            existing = ArtifactReceipt.model_validate(existing_payload)
            _validate_artifact_receipt_intent(existing, pending)
            pending = existing
        updates.append(
            pending.model_copy(
                update={
                    "status": ArtifactReceiptStatus.VERIFIED,
                    "checkpoint_revision": (
                        pending.checkpoint_revision or checkpoint.revision
                    ),
                    "verification_detail": "host_checkpoint",
                    "verified_at": pending.verified_at or now,
                }
            )
        )
    return tuple(updates)


async def _mark_attempt_evidence_checkpointed_in_transaction(
    connection: Any,
    tables: PostgresEventTables,
    attempt_id: str,
    *,
    checkpoint_revision: int,
    now: datetime,
) -> None:
    payloads = (
        await connection.execute(
            select(tables.side_effect_evidence.c.record)
            .where(
                tables.side_effect_evidence.c.execution_attempt_id == attempt_id,
                tables.side_effect_evidence.c.checkpoint_revision.is_(None),
            )
            .with_for_update()
        )
    ).scalars().all()
    for payload in payloads:
        evidence = SideEffectEvidence.model_validate(payload).model_copy(
            update={
                "checkpoint_revision": checkpoint_revision,
                "updated_at": now,
            }
        )
        await _write_side_effect_evidence(connection, tables, evidence)


async def _mark_open_side_effects_unknown_in_transaction(
    connection: Any,
    tables: PostgresEventTables,
    attempt_id: str,
    *,
    now: datetime,
) -> None:
    payloads = (
        await connection.execute(
            select(tables.side_effect_evidence.c.record)
            .where(
                tables.side_effect_evidence.c.execution_attempt_id == attempt_id,
                tables.side_effect_evidence.c.status.in_(
                    (SideEffectStatus.STARTED.value, SideEffectStatus.WAITING.value)
                ),
            )
            .with_for_update()
        )
    ).scalars().all()
    for payload in payloads:
        evidence = SideEffectEvidence.model_validate(payload).model_copy(
            update={
                "status": SideEffectStatus.UNKNOWN,
                "result_status": "execution_interrupted",
                "completed_at": now,
                "updated_at": now,
            }
        )
        await _write_side_effect_evidence(connection, tables, evidence)


async def _write_artifact_receipt(
    connection: Any,
    tables: PostgresEventTables,
    receipt: ArtifactReceipt,
) -> None:
    statement = postgresql_insert(tables.artifact_receipts).values(
        **_artifact_receipt_columns(receipt)
    )
    await connection.execute(
        statement.on_conflict_do_update(
            index_elements=[tables.artifact_receipts.c.artifact_id],
            set_=_artifact_receipt_columns(receipt),
        )
    )


async def _write_side_effect_evidence(
    connection: Any,
    tables: PostgresEventTables,
    evidence: SideEffectEvidence,
) -> None:
    await connection.execute(
        update(tables.side_effect_evidence)
        .where(tables.side_effect_evidence.c.evidence_id == evidence.evidence_id)
        .values(**_side_effect_evidence_columns(evidence))
    )


async def _append_event_in_transaction(
    connection: Any,
    tables: PostgresEventTables,
    run_id: str,
    kind: str,
    payload: Mapping[str, Any],
    *,
    turn: int | None,
) -> int:
    normalized_payload = _json_payload(payload)
    sequence_insert = postgresql_insert(tables.sequences).values(
        run_id=run_id,
        last_sequence=1,
    )
    allocate_sequence = sequence_insert.on_conflict_do_update(
        index_elements=[tables.sequences.c.run_id],
        set_={"last_sequence": tables.sequences.c.last_sequence + 1},
    ).returning(tables.sequences.c.last_sequence)
    sequence = int((await connection.execute(allocate_sequence)).scalar_one())
    await connection.execute(
        tables.events.insert().values(
            run_id=run_id,
            sequence=sequence,
            kind=kind,
            turn=turn,
            payload=normalized_payload,
        )
    )
    return sequence


def _distributed_terminal_event_kind(
    target_kind: ExecutionTargetKind,
    status: str,
) -> str:
    if status not in {"waiting", "succeeded", "failed", "cancelled"}:
        raise ValueError(f"unsupported terminal execution status '{status}'")
    prefix = "run" if target_kind is ExecutionTargetKind.AGENT_RUN else "flow"
    return f"{prefix}.{status}"


def _distributed_terminal_event_payload(
    attempt_id: str | None,
    *,
    target_kind: ExecutionTargetKind,
    status: str,
    output: Any | None,
    text: str | None,
    error: str | None,
    checkpoint_revision: int | None,
    pending_input: Mapping[str, Any] | None = None,
    waiting_child_run_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if attempt_id is not None:
        payload["execution_attempt_id"] = attempt_id
    if status == "succeeded":
        if target_kind is ExecutionTargetKind.AGENT_RUN:
            payload.update(output=_finite_json_value(output), text=text or "")
        else:
            payload["output"] = text if text is not None else _postgres_flow_output(output)
    elif status in {"failed", "cancelled"}:
        payload["error"] = error
        if target_kind is ExecutionTargetKind.AGENT_RUN:
            payload["text"] = text or ""
    else:
        payload["checkpoint_revision"] = checkpoint_revision
        if pending_input is not None:
            normalized = _finite_json_mapping(pending_input)
            input_kind = normalized.pop("kind", None)
            payload.update(normalized)
            payload["input_kind"] = input_kind
        if waiting_child_run_id is not None:
            payload["agent_run_id"] = waiting_child_run_id
    return payload


def _postgres_flow_output(value: Any | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        output = value.get("output")
        return output if isinstance(output, str) else None
    raise ValueError("Flow output must be a string or an object containing output")


async def _database_now(connection: Any) -> datetime:
    value = (await connection.execute(select(func.clock_timestamp()))).scalar_one()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RuntimeError("PostgreSQL clock_timestamp() did not return an aware datetime")
    return value.astimezone(UTC)


def _execution_request_columns(record: ExecutionRequest) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "operation": record.operation.value,
        "status": record.status.value,
        "runtime_route": record.runtime_route,
        "idempotency_key": record.idempotency_key,
        "expected_target_revision": record.expected_target_revision,
        "available_at": record.available_at,
        "claimed_attempt_id": record.claimed_attempt_id,
        "record": _finite_json_mapping(record.model_dump(mode="json")),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _execution_attempt_columns(record: ExecutionAttempt) -> dict[str, Any]:
    return {
        "attempt_id": record.attempt_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "request_id": record.request_id,
        "attempt_number": record.attempt_number,
        "status": record.status.value,
        "owner_id": record.owner_id,
        "fencing_token": record.fencing_token,
        "runtime_build_id": record.runtime_build_id,
        "heartbeat_at": record.heartbeat_at,
        "lease_expires_at": record.lease_expires_at,
        "record": _finite_json_mapping(record.model_dump(mode="json")),
        "created_at": record.claimed_at,
        "updated_at": record.ended_at or record.heartbeat_at,
    }


def _recovery_decision_columns(record: RecoveryDecisionRecord) -> dict[str, Any]:
    return {
        "decision_id": record.decision_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "decision": record.decision.value,
        "idempotency_key": record.idempotency_key,
        "record": _finite_json_mapping(record.model_dump(mode="json")),
        "created_at": record.created_at,
    }


def _artifact_receipt_columns(record: ArtifactReceipt) -> dict[str, Any]:
    return {
        "artifact_id": record.artifact_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "execution_attempt_id": record.execution_attempt_id,
        "status": record.status.value,
        "checkpoint_revision": record.checkpoint_revision,
        "record": _finite_json_mapping(record.model_dump(mode="json")),
        "created_at": record.recorded_at,
        "updated_at": record.verified_at or record.recorded_at,
    }


def _side_effect_evidence_columns(record: SideEffectEvidence) -> dict[str, Any]:
    return {
        "evidence_id": record.evidence_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "execution_attempt_id": record.execution_attempt_id,
        "operation_id": record.operation_id,
        "side_effect": record.side_effect.value,
        "status": record.status.value,
        "checkpoint_revision": record.checkpoint_revision,
        "record": _finite_json_mapping(record.model_dump(mode="json")),
        "created_at": record.started_at,
        "updated_at": record.updated_at,
    }


def _finite_json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _finite_json_value(dict(value))
    if not isinstance(result, dict):
        raise ValueError("Execution record must be a JSON object")
    return result


def _finite_json_value(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Execution values must contain only finite JSON") from exc


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


def _flow_run_columns(
    record: FlowRunRecord,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "flow_run_id": record.flow_run_id,
        "flow_name": record.flow_name,
        "flow_id": record.flow_id,
        "flow_kind": record.flow_kind.value,
        "definition_fingerprint": record.definition_fingerprint,
        "runtime_build_id": record.runtime_build_id,
        "runtime_route": record.runtime_route,
        "status": record.status.value,
        "revision": record.revision,
        "cancel_requested": record.cancel_requested,
        "current_request_id": record.current_request_id,
        "current_attempt_id": record.current_attempt_id,
        "checkpoint_revision": record.checkpoint_revision,
        "payload": payload if payload is not None else flow_record_json(record),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _flow_checkpoint_columns(record: FlowCheckpoint) -> dict[str, Any]:
    return {
        "flow_run_id": record.flow_run_id,
        "flow_id": record.flow_id,
        "flow_kind": record.flow_kind.value,
        "definition_fingerprint": record.definition_fingerprint,
        "revision": record.revision,
        "payload": flow_checkpoint_json(record),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _validate_run_lease(owner_id: str, lease_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Run lease owner_id must not be blank")
    if lease_seconds <= 0:
        raise ValueError("Run lease_seconds must be greater than zero")


def _validate_current_attempt_target(
    attempt: ExecutionAttempt,
    target: AgentRunRecord | FlowRunRecord,
) -> None:
    if (
        target.status.value != "running"
        or target.current_attempt_id != attempt.attempt_id
    ):
        raise ExecutionLeaseLostError(
            "ExecutionAttempt is no longer current for its target"
        )


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
