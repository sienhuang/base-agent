"""PostgreSQL metadata-only Flow side-effect ledger."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Self
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from base_agent.flows.repository import FlowRunNotFoundError
from base_agent.flows.side_effects import (
    FlowSideEffect,
    FlowSideEffectConflictError,
    FlowSideEffectLedger,
    FlowSideEffectNotFoundError,
    FlowSideEffectPhase,
    FlowSideEffectRevisionConflictError,
    _validate_transition,
)
from base_agent.models.run import utc_now
from base_agent.stores.postgres.schema import PostgresTables, build_tables


class PostgresFlowSideEffectLedger:
    """Persist side-effect intent and transitions without business payloads."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.engine = engine
        self.tables: PostgresTables = build_tables(schema)
        self._clock = clock
        self._owns_engine = False

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
        ledger = cls(engine, schema=schema, clock=clock)
        ledger._owns_engine = True
        return ledger

    async def create_schema(self) -> None:
        """Create missing tables; production deployments should use migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(self.tables.metadata.create_all)

    async def close(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    async def prepare(self, effect: FlowSideEffect) -> FlowSideEffect:
        if effect.phase is not FlowSideEffectPhase.PREPARED or effect.revision != 1:
            raise ValueError("new side-effect evidence must be prepared at revision 1")
        async with self.engine.begin() as connection:
            run_exists = (
                await connection.execute(
                    select(self.tables.flow_runs.c.id).where(
                        self.tables.flow_runs.c.id == effect.flow_run_id
                    )
                )
            ).scalar_one_or_none()
            if run_exists is None:
                raise FlowRunNotFoundError(
                    f"Flow Run '{effect.flow_run_id}' was not found"
                )
            payload = (
                await connection.execute(
                    postgresql_insert(self.tables.flow_side_effects)
                    .values(**_effect_values(effect))
                    .on_conflict_do_nothing(
                        constraint="uq_base_agent_flow_side_effect_operation"
                    )
                    .returning(self.tables.flow_side_effects.c.payload)
                )
            ).scalar_one_or_none()
            if payload is not None:
                return FlowSideEffect.model_validate(payload)
            existing = await self._find_operation(connection, effect)
        assert existing is not None
        if not existing.has_same_intent(effect):
            raise FlowSideEffectConflictError(
                f"side-effect operation '{effect.operation_key}' "
                "has different intent"
            )
        return existing

    async def get(self, effect_id: UUID) -> FlowSideEffect:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(self.tables.flow_side_effects.c.payload).where(
                        self.tables.flow_side_effects.c.id == effect_id
                    )
                )
            ).scalar_one_or_none()
        if payload is None:
            raise FlowSideEffectNotFoundError(
                f"side-effect evidence '{effect_id}' was not found"
            )
        return FlowSideEffect.model_validate(payload)

    async def list_for_invocation(
        self,
        invocation_id: UUID,
    ) -> tuple[FlowSideEffect, ...]:
        statement = (
            select(self.tables.flow_side_effects.c.payload)
            .where(self.tables.flow_side_effects.c.invocation_id == invocation_id)
            .order_by(
                self.tables.flow_side_effects.c.created_at,
                self.tables.flow_side_effects.c.id,
            )
        )
        async with self.engine.connect() as connection:
            payloads = (await connection.execute(statement)).scalars().all()
        return tuple(FlowSideEffect.model_validate(payload) for payload in payloads)

    async def mark_started(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect:
        return await self._transition(
            effect_id,
            expected_revision=expected_revision,
            target=FlowSideEffectPhase.STARTED,
        )

    async def confirm(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect:
        return await self._transition(
            effect_id,
            expected_revision=expected_revision,
            target=FlowSideEffectPhase.CONFIRMED,
        )

    async def abort(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect:
        return await self._transition(
            effect_id,
            expected_revision=expected_revision,
            target=FlowSideEffectPhase.ABORTED,
        )

    async def _transition(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
        target: FlowSideEffectPhase,
    ) -> FlowSideEffect:
        async with self.engine.begin() as connection:
            payload = (
                await connection.execute(
                    select(self.tables.flow_side_effects.c.payload)
                    .where(self.tables.flow_side_effects.c.id == effect_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if payload is None:
                raise FlowSideEffectNotFoundError(
                    f"side-effect evidence '{effect_id}' was not found"
                )
            current = FlowSideEffect.model_validate(payload)
            if current.revision != expected_revision:
                raise FlowSideEffectRevisionConflictError(
                    f"side-effect evidence '{effect_id}' expected revision "
                    f"{expected_revision}, found {current.revision}"
                )
            _validate_transition(current.phase, target)
            replacement = current.model_copy(
                update={
                    "phase": target,
                    "revision": current.revision + 1,
                    "updated_at": self._clock(),
                }
            )
            await connection.execute(
                update(self.tables.flow_side_effects)
                .where(self.tables.flow_side_effects.c.id == effect_id)
                .values(**_effect_values(replacement))
            )
        return replacement

    async def _find_operation(
        self,
        connection: AsyncConnection,
        effect: FlowSideEffect,
    ) -> FlowSideEffect | None:
        payload = (
            await connection.execute(
                select(self.tables.flow_side_effects.c.payload).where(
                    self.tables.flow_side_effects.c.run_id == effect.flow_run_id,
                    self.tables.flow_side_effects.c.invocation_id
                    == effect.invocation_id,
                    self.tables.flow_side_effects.c.operation_key
                    == effect.operation_key,
                )
            )
        ).scalar_one_or_none()
        return (
            FlowSideEffect.model_validate(payload)
            if payload is not None
            else None
        )


def _effect_values(effect: FlowSideEffect) -> dict[str, Any]:
    return {
        "id": effect.id,
        "run_id": effect.flow_run_id,
        "invocation_id": effect.invocation_id,
        "operation_key": effect.operation_key,
        "operation_name": effect.operation_name,
        "retry_mode": effect.retry_mode.value,
        "phase": effect.phase.value,
        "revision": effect.revision,
        "created_at": effect.created_at,
        "updated_at": effect.updated_at,
        "payload": effect.model_dump(mode="json"),
    }


_ledger_contract: type[FlowSideEffectLedger] = PostgresFlowSideEffectLedger
