"""Durable PostgreSQL repository for Flow snapshots and ordered events."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from base_agent.flows.lease import (
    FlowExecutionLease,
    FlowLeaseLostError,
    FlowLeaseUnavailableError,
)
from base_agent.flows.lifecycle import FlowRunState
from base_agent.flows.repository import (
    FlowEventDraft,
    FlowRepository,
    FlowRevisionConflictError,
    FlowRunAlreadyExistsError,
    FlowRunNotFoundError,
    validate_flow_replacement,
)
from base_agent.models import RunStatus, RuntimeEvent
from base_agent.models.run import utc_now
from base_agent.stores.postgres.schema import PostgresTables, build_tables

_TERMINAL_FLOW_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
    RunStatus.LIMIT_REACHED,
}


class PostgresFlowRepository:
    """Persist one Flow aggregate and its events in the same transaction."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.engine = engine
        self.tables: PostgresTables = build_tables(schema)
        self._owns_engine = False
        self._clock = clock

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        clock: Callable[[], datetime] = utc_now,
        **engine_options: Any,
    ) -> Self:
        engine = create_async_engine(url, **engine_options)
        repository = cls(engine, schema=schema, clock=clock)
        repository._owns_engine = True
        return repository

    async def create_schema(self) -> None:
        """Create missing tables; production deployments should use migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(self.tables.metadata.create_all)

    async def close(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    async def create(
        self,
        state: FlowRunState,
        *,
        events: tuple[FlowEventDraft, ...],
    ) -> tuple[RuntimeEvent, ...]:
        if state.revision != 1:
            raise ValueError("a new Flow Run must start at revision 1")
        if not events:
            raise ValueError("creating a Flow Run requires at least one event")
        persisted = _materialize_events(state.run_id, events, start_sequence=1)
        try:
            async with self.engine.begin() as connection:
                await connection.execute(
                    insert(self.tables.flow_runs).values(**_flow_values(state))
                )
                await self._insert_events(connection, persisted)
        except IntegrityError as exc:
            raise FlowRunAlreadyExistsError(
                f"Flow Run '{state.run_id}' already exists"
            ) from exc
        return _copy_events(persisted)

    async def get(self, run_id: UUID) -> FlowRunState:
        statement = select(self.tables.flow_runs.c.payload).where(
            self.tables.flow_runs.c.id == run_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise FlowRunNotFoundError(f"Flow Run '{run_id}' was not found")
        return FlowRunState.model_validate(payload)

    async def commit(
        self,
        state: FlowRunState,
        *,
        expected_revision: int,
        events: tuple[FlowEventDraft, ...],
        execution_token: UUID | None = None,
    ) -> tuple[RuntimeEvent, ...]:
        if not events:
            raise ValueError("committing a Flow Run requires at least one event")
        async with self.engine.begin() as connection:
            current = await self._lock_state(connection, state.run_id)
            await self._validate_execution_token(
                connection,
                state.run_id,
                execution_token,
                now=self._clock(),
            )
            validate_flow_replacement(
                current,
                state,
                expected_revision=expected_revision,
            )
            next_sequence = await self._next_sequence(connection, state.run_id)
            persisted = _materialize_events(
                state.run_id,
                events,
                start_sequence=next_sequence,
            )
            result = await connection.execute(
                update(self.tables.flow_runs)
                .where(
                    self.tables.flow_runs.c.id == state.run_id,
                    self.tables.flow_runs.c.revision == expected_revision,
                )
                .values(**_flow_values(state))
            )
            if result.rowcount != 1:
                raise FlowRevisionConflictError(
                    f"Flow Run '{state.run_id}' changed while committing"
                )
            await self._insert_events(connection, persisted)
        return _copy_events(persisted)

    async def append_events(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        events: tuple[FlowEventDraft, ...],
        execution_token: UUID | None = None,
    ) -> tuple[RuntimeEvent, ...]:
        if not events:
            raise ValueError("appending Flow events requires at least one event")
        async with self.engine.begin() as connection:
            current = await self._lock_state(connection, run_id)
            await self._validate_execution_token(
                connection,
                run_id,
                execution_token,
                now=self._clock(),
            )
            if current.revision != expected_revision:
                raise FlowRevisionConflictError(
                    f"Flow Run '{run_id}' revision conflict: "
                    f"expected {expected_revision}, found {current.revision}"
                )
            next_sequence = await self._next_sequence(connection, run_id)
            persisted = _materialize_events(
                run_id,
                events,
                start_sequence=next_sequence,
            )
            await self._insert_events(connection, persisted)
        return _copy_events(persisted)

    async def events(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        statement = (
            select(self.tables.flow_events.c.payload)
            .where(self.tables.flow_events.c.run_id == run_id)
            .order_by(self.tables.flow_events.c.sequence)
        )
        async with self.engine.connect() as connection:
            exists = (
                await connection.execute(
                    select(self.tables.flow_runs.c.id).where(
                        self.tables.flow_runs.c.id == run_id
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise FlowRunNotFoundError(f"Flow Run '{run_id}' was not found")
            payloads = (await connection.execute(statement)).scalars().all()
        return tuple(RuntimeEvent.model_validate(payload) for payload in payloads)

    async def acquire_execution(
        self,
        run_id: UUID,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowExecutionLease:
        _validate_lease_arguments(owner_id, ttl_seconds)
        acquired_at = now or self._clock()
        async with self.engine.begin() as connection:
            state = await self._lock_state(connection, run_id)
            if state.status in _TERMINAL_FLOW_STATUSES:
                raise FlowLeaseUnavailableError(
                    f"terminal Flow Run '{run_id}' cannot be claimed"
                )
            prior = await self._lease_row(connection, run_id)
            if (
                prior is not None
                and prior["active"]
                and prior["expires_at"] > acquired_at
            ):
                raise FlowLeaseUnavailableError(
                    f"Flow Run '{run_id}' is leased by '{prior['owner_id']}'"
                )
            lease = FlowExecutionLease(
                run_id=run_id,
                token=uuid4(),
                owner_id=owner_id,
                attempt=(int(prior["attempt"]) + 1 if prior is not None else 1),
                acquired_at=acquired_at,
                heartbeat_at=acquired_at,
                expires_at=acquired_at + timedelta(seconds=ttl_seconds),
            )
            values = _lease_values(lease, active=True)
            if prior is None:
                await connection.execute(
                    insert(self.tables.flow_leases).values(**values)
                )
            else:
                await connection.execute(
                    update(self.tables.flow_leases)
                    .where(self.tables.flow_leases.c.run_id == run_id)
                    .values(**values)
                )
        return lease

    async def renew_execution(
        self,
        lease: FlowExecutionLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowExecutionLease:
        _validate_lease_arguments(lease.owner_id, ttl_seconds)
        heartbeat_at = now or self._clock()
        async with self.engine.begin() as connection:
            await self._lock_state(connection, lease.run_id)
            row = await self._lease_row(connection, lease.run_id)
            if (
                row is None
                or not row["active"]
                or row["token"] != lease.token
                or row["expires_at"] <= heartbeat_at
            ):
                raise FlowLeaseLostError(
                    f"Flow execution lease for '{lease.run_id}' was lost"
                )
            current = _lease_from_row(row)
            renewed = current.model_copy(
                update={
                    "heartbeat_at": heartbeat_at,
                    "expires_at": heartbeat_at + timedelta(seconds=ttl_seconds),
                }
            )
            await connection.execute(
                update(self.tables.flow_leases)
                .where(self.tables.flow_leases.c.run_id == lease.run_id)
                .values(**_lease_values(renewed, active=True))
            )
        return renewed

    async def release_execution(self, lease: FlowExecutionLease) -> None:
        async with self.engine.begin() as connection:
            await self._lock_state(connection, lease.run_id)
            row = await self._lease_row(connection, lease.run_id)
            if row is None or row["token"] != lease.token:
                raise FlowLeaseLostError(
                    f"Flow execution lease for '{lease.run_id}' was lost"
                )
            if row["active"]:
                await connection.execute(
                    update(self.tables.flow_leases)
                    .where(self.tables.flow_leases.c.run_id == lease.run_id)
                    .values(active=False)
                )

    async def _lock_state(
        self,
        connection: AsyncConnection,
        run_id: UUID,
    ) -> FlowRunState:
        payload = (
            await connection.execute(
                select(self.tables.flow_runs.c.payload)
                .where(self.tables.flow_runs.c.id == run_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if payload is None:
            raise FlowRunNotFoundError(f"Flow Run '{run_id}' was not found")
        return FlowRunState.model_validate(payload)

    async def _next_sequence(
        self,
        connection: AsyncConnection,
        run_id: UUID,
    ) -> int:
        current = (
            await connection.execute(
                select(func.max(self.tables.flow_events.c.sequence)).where(
                    self.tables.flow_events.c.run_id == run_id
                )
            )
        ).scalar_one()
        return int(current or 0) + 1

    async def _lease_row(
        self,
        connection: AsyncConnection,
        run_id: UUID,
    ) -> RowMapping | None:
        return (
            await connection.execute(
                select(self.tables.flow_leases)
                .where(self.tables.flow_leases.c.run_id == run_id)
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def _validate_execution_token(
        self,
        connection: AsyncConnection,
        run_id: UUID,
        execution_token: UUID | None,
        *,
        now: datetime,
    ) -> None:
        row = await self._lease_row(connection, run_id)
        if row is None:
            if execution_token is not None:
                raise FlowLeaseLostError(
                    f"Flow Run '{run_id}' has no execution lease"
                )
            return
        if (
            execution_token is None
            or not row["active"]
            or row["token"] != execution_token
            or row["expires_at"] <= now
        ):
            raise FlowLeaseLostError(
                f"Flow execution lease for '{run_id}' is not valid"
            )

    async def _insert_events(
        self,
        connection: AsyncConnection,
        events: tuple[RuntimeEvent, ...],
    ) -> None:
        await connection.execute(
            insert(self.tables.flow_events),
            [
                {
                    "run_id": event.run_id,
                    "sequence": event.sequence,
                    "event_id": event.id,
                    "event_type": event.type.value,
                    "created_at": event.timestamp,
                    "payload": _model_payload(event),
                }
                for event in events
            ],
        )


def _materialize_events(
    run_id: UUID,
    drafts: tuple[FlowEventDraft, ...],
    *,
    start_sequence: int,
) -> tuple[RuntimeEvent, ...]:
    return tuple(
        RuntimeEvent(
            run_id=run_id,
            sequence=start_sequence + offset,
            type=draft.type,
            data=draft.data,
        )
        for offset, draft in enumerate(drafts)
    )


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _flow_values(state: FlowRunState) -> dict[str, Any]:
    return {
        "id": state.run_id,
        "revision": state.revision,
        "status": state.status.value,
        "updated_at": state.updated_at,
        "payload": _model_payload(state),
    }


def _lease_values(
    lease: FlowExecutionLease,
    *,
    active: bool,
) -> dict[str, Any]:
    return {
        "run_id": lease.run_id,
        "token": lease.token,
        "owner_id": lease.owner_id,
        "attempt": lease.attempt,
        "active": active,
        "acquired_at": lease.acquired_at,
        "heartbeat_at": lease.heartbeat_at,
        "expires_at": lease.expires_at,
    }


def _lease_from_row(row: RowMapping) -> FlowExecutionLease:
    return FlowExecutionLease(
        run_id=row["run_id"],
        token=row["token"],
        owner_id=row["owner_id"],
        attempt=row["attempt"],
        acquired_at=row["acquired_at"],
        heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"],
    )


def _copy_events(events: tuple[RuntimeEvent, ...]) -> tuple[RuntimeEvent, ...]:
    return tuple(event.model_copy(deep=True) for event in events)


_flow_repository_contract: type[FlowRepository] = PostgresFlowRepository


def _validate_lease_arguments(owner_id: str, ttl_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Flow lease owner_id must not be blank")
    if len(owner_id) > 256:
        raise ValueError("Flow lease owner_id must not exceed 256 characters")
    if ttl_seconds <= 0:
        raise ValueError("Flow lease ttl_seconds must be greater than zero")
