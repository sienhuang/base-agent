"""PostgreSQL durable work source for Flow worker delivery."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Self
from uuid import UUID, uuid4

from pydantic import BaseModel
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from base_agent.flows.repository import FlowRunNotFoundError
from base_agent.flows.work import (
    FlowWorkCommand,
    FlowWorkDeliveryLostError,
    FlowWorkIdempotencyConflictError,
    FlowWorkItem,
    FlowWorkNotFoundError,
    FlowWorkReview,
    FlowWorkReviewConflictError,
    FlowWorkReviewDecision,
    FlowWorkReviewResult,
    FlowWorkReviewStateError,
    FlowWorkReviewStore,
    FlowWorkSource,
    FlowWorkStatus,
)
from base_agent.models.run import utc_now
from base_agent.stores.postgres.schema import PostgresTables, build_tables


class PostgresFlowWorkSource:
    """Persist and fence idempotently enqueued Flow work deliveries."""

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
        source = cls(engine, schema=schema, clock=clock)
        source._owns_engine = True
        return source

    async def create_schema(self) -> None:
        """Create missing tables; production deployments should use migrations."""
        async with self.engine.begin() as connection:
            await connection.run_sync(self.tables.metadata.create_all)

    async def close(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    async def enqueue(self, command: FlowWorkCommand) -> FlowWorkItem:
        now = self._clock()
        item = FlowWorkItem(
            command=command,
            available_at=now,
            updated_at=now,
        )
        async with self.engine.begin() as connection:
            exists = (
                await connection.execute(
                    select(self.tables.flow_runs.c.id).where(
                        self.tables.flow_runs.c.id == command.run_id
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise FlowRunNotFoundError(
                    f"Flow Run '{command.run_id}' was not found"
                )
            payload = (
                await connection.execute(
                    postgresql_insert(self.tables.flow_work_items)
                    .values(**_work_values(item))
                    .on_conflict_do_nothing(
                        index_elements=[
                            self.tables.flow_work_items.c.idempotency_key
                        ]
                    )
                    .returning(self.tables.flow_work_items.c.payload)
                )
            ).scalar_one_or_none()
            if payload is not None:
                return FlowWorkItem.model_validate(payload)
            existing_payload = (
                await connection.execute(
                    select(self.tables.flow_work_items.c.payload).where(
                        self.tables.flow_work_items.c.idempotency_key
                        == command.idempotency_key
                    )
                )
            ).scalar_one()
        existing = FlowWorkItem.model_validate(existing_payload)
        if not existing.command.has_same_intent(command):
            raise FlowWorkIdempotencyConflictError(
                f"Flow work idempotency key "
                f"'{command.idempotency_key}' has different intent"
            )
        return existing

    async def get(self, work_id: UUID) -> FlowWorkItem:
        async with self.engine.connect() as connection:
            payload = (
                await connection.execute(
                    select(self.tables.flow_work_items.c.payload).where(
                        self.tables.flow_work_items.c.id == work_id
                    )
                )
            ).scalar_one_or_none()
        if payload is None:
            raise FlowWorkNotFoundError(
                f"Flow work item '{work_id}' was not found"
            )
        return FlowWorkItem.model_validate(payload)

    async def list_blocked(
        self,
        *,
        limit: int = 100,
    ) -> tuple[FlowWorkItem, ...]:
        _validate_list_limit(limit)
        statement = (
            select(self.tables.flow_work_items.c.payload)
            .where(
                self.tables.flow_work_items.c.status
                == FlowWorkStatus.BLOCKED.value
            )
            .order_by(
                self.tables.flow_work_items.c.updated_at,
                self.tables.flow_work_items.c.id,
            )
            .limit(limit)
        )
        async with self.engine.connect() as connection:
            payloads = (await connection.execute(statement)).scalars().all()
        return tuple(FlowWorkItem.model_validate(payload) for payload in payloads)

    async def review(self, review: FlowWorkReview) -> FlowWorkReviewResult:
        async with self.engine.begin() as connection:
            existing = await self._find_review(
                connection,
                review.idempotency_key,
            )
            if existing is not None:
                _validate_review_intent(existing, review)
                return FlowWorkReviewResult(
                    review=existing,
                    item=await self._lock_item(connection, existing.work_id),
                )

            current = await self._lock_item(connection, review.work_id)
            existing = await self._find_review(
                connection,
                review.idempotency_key,
            )
            if existing is not None:
                _validate_review_intent(existing, review)
                return FlowWorkReviewResult(review=existing, item=current)
            if current.status is not FlowWorkStatus.BLOCKED:
                raise FlowWorkReviewStateError(
                    f"Flow work item '{review.work_id}' is not blocked"
                )

            reviewed_at = self._clock()
            persisted_review = review.model_copy(
                update={"created_at": reviewed_at}
            )
            inserted_payload = (
                await connection.execute(
                    postgresql_insert(self.tables.flow_work_reviews)
                    .values(**_review_values(persisted_review))
                    .on_conflict_do_nothing(
                        index_elements=[
                            self.tables.flow_work_reviews.c.idempotency_key
                        ]
                    )
                    .returning(self.tables.flow_work_reviews.c.payload)
                )
            ).scalar_one_or_none()
            if inserted_payload is None:
                conflicting = await self._find_review(
                    connection,
                    review.idempotency_key,
                )
                assert conflicting is not None
                _validate_review_intent(conflicting, review)
                return FlowWorkReviewResult(review=conflicting, item=current)

            replacement = _apply_review(
                current,
                persisted_review,
                now=reviewed_at,
            )
            await self._update_item(connection, replacement)
        return FlowWorkReviewResult(
            review=FlowWorkReview.model_validate(inserted_payload),
            item=replacement,
        )

    async def list_reviews(self, work_id: UUID) -> tuple[FlowWorkReview, ...]:
        async with self.engine.connect() as connection:
            exists = (
                await connection.execute(
                    select(self.tables.flow_work_items.c.id).where(
                        self.tables.flow_work_items.c.id == work_id
                    )
                )
            ).scalar_one_or_none()
            if exists is None:
                raise FlowWorkNotFoundError(
                    f"Flow work item '{work_id}' was not found"
                )
            payloads = (
                await connection.execute(
                    select(self.tables.flow_work_reviews.c.payload)
                    .where(self.tables.flow_work_reviews.c.work_id == work_id)
                    .order_by(
                        self.tables.flow_work_reviews.c.created_at,
                        self.tables.flow_work_reviews.c.id,
                    )
                )
            ).scalars().all()
        return tuple(FlowWorkReview.model_validate(payload) for payload in payloads)

    async def claim(
        self,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowWorkItem | None:
        _validate_delivery_arguments(owner_id, ttl_seconds)
        claimed_at = now or self._clock()
        eligible = or_(
            and_(
                self.tables.flow_work_items.c.status
                == FlowWorkStatus.PENDING.value,
                self.tables.flow_work_items.c.available_at <= claimed_at,
            ),
            and_(
                self.tables.flow_work_items.c.status
                == FlowWorkStatus.CLAIMED.value,
                self.tables.flow_work_items.c.lease_expires_at <= claimed_at,
            ),
        )
        async with self.engine.begin() as connection:
            payload = (
                await connection.execute(
                    select(self.tables.flow_work_items.c.payload)
                    .where(eligible)
                    .order_by(
                        self.tables.flow_work_items.c.available_at,
                        self.tables.flow_work_items.c.created_at,
                        self.tables.flow_work_items.c.id,
                    )
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if payload is None:
                return None
            current = FlowWorkItem.model_validate(payload)
            claimed = current.model_copy(
                update={
                    "status": FlowWorkStatus.CLAIMED,
                    "attempt": current.attempt + 1,
                    "owner_id": owner_id,
                    "delivery_token": uuid4(),
                    "claimed_at": claimed_at,
                    "lease_expires_at": claimed_at
                    + timedelta(seconds=ttl_seconds),
                    "updated_at": claimed_at,
                }
            )
            await connection.execute(
                update(self.tables.flow_work_items)
                .where(self.tables.flow_work_items.c.id == claimed.id)
                .values(**_work_values(claimed))
            )
        return claimed

    async def complete(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        now: datetime | None = None,
    ) -> FlowWorkItem:
        completed_at = now or self._clock()
        async with self.engine.begin() as connection:
            current = await self._lock_item(connection, work_id)
            _validate_delivery(
                current,
                delivery_token,
                now=completed_at,
                idempotent_status=FlowWorkStatus.COMPLETED,
            )
            if current.status is FlowWorkStatus.COMPLETED:
                return current
            completed = current.model_copy(
                update={
                    "status": FlowWorkStatus.COMPLETED,
                    "owner_id": None,
                    "last_delivery_token": current.delivery_token,
                    "delivery_token": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "updated_at": completed_at,
                }
            )
            await self._update_item(connection, completed)
        return completed

    async def renew(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowWorkItem:
        if ttl_seconds <= 0:
            raise ValueError("Flow work ttl_seconds must be greater than zero")
        renewed_at = now or self._clock()
        async with self.engine.begin() as connection:
            current = await self._lock_item(connection, work_id)
            _validate_active_delivery(
                current,
                delivery_token,
                now=renewed_at,
            )
            renewed = current.model_copy(
                update={
                    "lease_expires_at": renewed_at
                    + timedelta(seconds=ttl_seconds),
                    "updated_at": renewed_at,
                }
            )
            await self._update_item(connection, renewed)
        return renewed

    async def block(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        reason_code: str,
        now: datetime | None = None,
    ) -> FlowWorkItem:
        _validate_reason_code(reason_code)
        blocked_at = now or self._clock()
        async with self.engine.begin() as connection:
            current = await self._lock_item(connection, work_id)
            _validate_delivery(
                current,
                delivery_token,
                now=blocked_at,
                idempotent_status=FlowWorkStatus.BLOCKED,
            )
            if current.status is FlowWorkStatus.BLOCKED:
                return current
            blocked = current.model_copy(
                update={
                    "status": FlowWorkStatus.BLOCKED,
                    "owner_id": None,
                    "last_delivery_token": current.delivery_token,
                    "delivery_token": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "blocked_reason": reason_code,
                    "updated_at": blocked_at,
                }
            )
            await self._update_item(connection, blocked)
        return blocked

    async def retry(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        delay_seconds: float = 0,
        error_type: str | None = None,
        now: datetime | None = None,
    ) -> FlowWorkItem:
        _validate_retry(delay_seconds, error_type)
        retried_at = now or self._clock()
        async with self.engine.begin() as connection:
            current = await self._lock_item(connection, work_id)
            _validate_delivery(
                current,
                delivery_token,
                now=retried_at,
                idempotent_status=FlowWorkStatus.PENDING,
            )
            if current.status is FlowWorkStatus.PENDING:
                return current
            pending = current.model_copy(
                update={
                    "status": FlowWorkStatus.PENDING,
                    "available_at": retried_at + timedelta(seconds=delay_seconds),
                    "owner_id": None,
                    "last_delivery_token": current.delivery_token,
                    "delivery_token": None,
                    "claimed_at": None,
                    "lease_expires_at": None,
                    "last_error_type": error_type,
                    "updated_at": retried_at,
                }
            )
            await self._update_item(connection, pending)
        return pending

    async def _lock_item(
        self,
        connection: AsyncConnection,
        work_id: UUID,
    ) -> FlowWorkItem:
        payload = (
            await connection.execute(
                select(self.tables.flow_work_items.c.payload)
                .where(self.tables.flow_work_items.c.id == work_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if payload is None:
            raise FlowWorkNotFoundError(
                f"Flow work item '{work_id}' was not found"
            )
        return FlowWorkItem.model_validate(payload)

    async def _update_item(
        self,
        connection: AsyncConnection,
        item: FlowWorkItem,
    ) -> None:
        await connection.execute(
            update(self.tables.flow_work_items)
            .where(self.tables.flow_work_items.c.id == item.id)
            .values(**_work_values(item))
        )

    async def _find_review(
        self,
        connection: AsyncConnection,
        idempotency_key: str,
    ) -> FlowWorkReview | None:
        payload = (
            await connection.execute(
                select(self.tables.flow_work_reviews.c.payload).where(
                    self.tables.flow_work_reviews.c.idempotency_key
                    == idempotency_key
                )
            )
        ).scalar_one_or_none()
        if payload is None:
            return None
        return FlowWorkReview.model_validate(payload)


def _work_values(item: FlowWorkItem) -> dict[str, Any]:
    command = item.command
    return {
        "id": item.id,
        "run_id": command.run_id,
        "idempotency_key": command.idempotency_key,
        "kind": command.kind.value,
        "status": item.status.value,
        "attempt": item.attempt,
        "available_at": item.available_at,
        "owner_id": item.owner_id,
        "delivery_token": item.delivery_token,
        "last_delivery_token": item.last_delivery_token,
        "claimed_at": item.claimed_at,
        "lease_expires_at": item.lease_expires_at,
        "last_error_type": item.last_error_type,
        "blocked_reason": item.blocked_reason,
        "created_at": command.created_at,
        "updated_at": item.updated_at,
        "payload": _model_payload(item),
    }


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _review_values(review: FlowWorkReview) -> dict[str, Any]:
    return {
        "id": review.id,
        "work_id": review.work_id,
        "decision": review.decision.value,
        "reviewer_id": review.reviewer_id,
        "reason_code": review.reason_code,
        "idempotency_key": review.idempotency_key,
        "delay_seconds": review.delay_seconds,
        "created_at": review.created_at,
        "payload": _model_payload(review),
    }


def _validate_delivery(
    current: FlowWorkItem,
    delivery_token: UUID,
    *,
    now: datetime,
    idempotent_status: FlowWorkStatus,
) -> None:
    if (
        current.status is idempotent_status
        and current.last_delivery_token == delivery_token
    ):
        return
    if (
        current.status is not FlowWorkStatus.CLAIMED
        or current.delivery_token != delivery_token
        or current.lease_expires_at is None
        or current.lease_expires_at <= now
    ):
        raise FlowWorkDeliveryLostError(
            f"Flow work delivery for '{current.id}' was lost"
        )


def _validate_active_delivery(
    current: FlowWorkItem,
    delivery_token: UUID,
    *,
    now: datetime,
) -> None:
    if (
        current.status is not FlowWorkStatus.CLAIMED
        or current.delivery_token != delivery_token
        or current.lease_expires_at is None
        or current.lease_expires_at <= now
    ):
        raise FlowWorkDeliveryLostError(
            f"Flow work delivery for '{current.id}' was lost"
        )


def _validate_delivery_arguments(owner_id: str, ttl_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Flow work owner_id must not be blank")
    if len(owner_id) > 256:
        raise ValueError("Flow work owner_id must not exceed 256 characters")
    if ttl_seconds <= 0:
        raise ValueError("Flow work ttl_seconds must be greater than zero")


def _validate_retry(delay_seconds: float, error_type: str | None) -> None:
    if delay_seconds < 0:
        raise ValueError("Flow work retry delay_seconds must not be negative")
    if error_type is not None and (not error_type.strip() or len(error_type) > 256):
        raise ValueError("Flow work retry error_type must be 1 to 256 characters")


def _validate_reason_code(reason_code: str) -> None:
    if not reason_code.strip() or len(reason_code) > 256:
        raise ValueError(
            "Flow work blocked reason_code must be 1 to 256 characters"
        )


def _validate_list_limit(limit: int) -> None:
    if not 1 <= limit <= 1_000:
        raise ValueError("Flow work list limit must be between 1 and 1000")


def _validate_review_intent(
    existing: FlowWorkReview,
    requested: FlowWorkReview,
) -> None:
    if not existing.has_same_intent(requested):
        raise FlowWorkReviewConflictError(
            f"Flow work review idempotency key "
            f"'{requested.idempotency_key}' has different intent"
        )


def _apply_review(
    current: FlowWorkItem,
    review: FlowWorkReview,
    *,
    now: datetime,
) -> FlowWorkItem:
    if review.decision is FlowWorkReviewDecision.APPROVE_RETRY:
        return current.model_copy(
            update={
                "status": FlowWorkStatus.PENDING,
                "available_at": now + timedelta(seconds=review.delay_seconds),
                "blocked_reason": None,
                "last_error_type": None,
                "updated_at": now,
            }
        )
    return current.model_copy(
        update={
            "status": FlowWorkStatus.DISCARDED,
            "blocked_reason": None,
            "updated_at": now,
        }
    )


_postgres_work_source_contract: type[FlowWorkSource] = PostgresFlowWorkSource
_postgres_review_store_contract: type[FlowWorkReviewStore] = (
    PostgresFlowWorkSource
)
