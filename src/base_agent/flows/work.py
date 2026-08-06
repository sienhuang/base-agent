"""Durable work-item contracts for scheduling Flow execution."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from base_agent.models.run import utc_now


class FlowWorkKind(StrEnum):
    EXECUTE = "execute"
    RESUME = "resume"
    CANCEL = "cancel"
    RECOVER = "recover"


class FlowWorkStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    DISCARDED = "discarded"


class FlowWorkReviewDecision(StrEnum):
    APPROVE_RETRY = "approve_retry"
    REJECT = "reject"


class FlowWorkIdempotencyConflictError(RuntimeError):
    """Raised when an idempotency key is reused for different work."""


class FlowWorkDeliveryLostError(RuntimeError):
    """Raised when a stale delivery token tries to settle a work item."""


class FlowWorkNotFoundError(KeyError):
    """Raised when a Flow work item cannot be found."""


class FlowWorkBlockedError(RuntimeError):
    """Signal that a claimed item requires explicit operator handling."""

    def __init__(self, reason_code: str) -> None:
        if not reason_code.strip() or len(reason_code) > 256:
            raise ValueError(
                "Flow work blocked reason_code must be 1 to 256 characters"
            )
        self.reason_code = reason_code
        super().__init__(reason_code)


class FlowWorkReviewConflictError(RuntimeError):
    """Raised when a review idempotency key has different intent."""


class FlowWorkReviewStateError(RuntimeError):
    """Raised when an operator reviews work that is no longer BLOCKED."""


class FlowWorkCommand(BaseModel):
    """One idempotently enqueued request to process a Flow Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    kind: FlowWorkKind = FlowWorkKind.EXECUTE
    idempotency_key: str = Field(min_length=1, max_length=256)
    data: dict[str, JsonValue] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)

    def has_same_intent(self, other: FlowWorkCommand) -> bool:
        return (
            self.run_id == other.run_id
            and self.kind is other.kind
            and self.data == other.data
        )


class FlowWorkItem(BaseModel):
    """Persisted scheduling state and current fenced delivery, if any."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: FlowWorkCommand
    status: FlowWorkStatus = FlowWorkStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    available_at: datetime = Field(default_factory=utc_now)
    owner_id: str | None = Field(default=None, min_length=1, max_length=256)
    delivery_token: UUID | None = None
    last_delivery_token: UUID | None = None
    claimed_at: datetime | None = None
    lease_expires_at: datetime | None = None
    last_error_type: str | None = Field(default=None, min_length=1, max_length=256)
    blocked_reason: str | None = Field(default=None, min_length=1, max_length=256)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_delivery(self) -> FlowWorkItem:
        delivery_fields = (
            self.owner_id,
            self.delivery_token,
            self.claimed_at,
            self.lease_expires_at,
        )
        if self.status is FlowWorkStatus.CLAIMED:
            if any(value is None for value in delivery_fields):
                raise ValueError("a claimed Flow work item requires delivery fields")
            if self.attempt < 1:
                raise ValueError("a claimed Flow work item requires an attempt")
        elif any(value is not None for value in delivery_fields):
            raise ValueError("only a claimed Flow work item may retain delivery fields")
        if self.status is FlowWorkStatus.BLOCKED:
            if self.blocked_reason is None:
                raise ValueError("a blocked Flow work item requires blocked_reason")
        elif self.blocked_reason is not None:
            raise ValueError("only a blocked Flow work item may retain blocked_reason")
        return self

    @property
    def id(self) -> UUID:
        return self.command.id


class FlowWorkReview(BaseModel):
    """Audited operator decision for one BLOCKED work item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    work_id: UUID
    decision: FlowWorkReviewDecision
    reviewer_id: str = Field(min_length=1, max_length=256)
    reason_code: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    delay_seconds: float = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_decision(self) -> FlowWorkReview:
        if (
            self.decision is FlowWorkReviewDecision.REJECT
            and self.delay_seconds != 0
        ):
            raise ValueError("a rejected Flow work item cannot have a retry delay")
        return self

    def has_same_intent(self, other: FlowWorkReview) -> bool:
        return (
            self.work_id == other.work_id
            and self.decision is other.decision
            and self.reviewer_id == other.reviewer_id
            and self.reason_code == other.reason_code
            and self.delay_seconds == other.delay_seconds
        )


class FlowWorkReviewResult(BaseModel):
    """Operator review plus the resulting current work snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    review: FlowWorkReview
    item: FlowWorkItem


class FlowWorkSource(Protocol):
    """Idempotently enqueue and fence deliveries of durable Flow work."""

    async def enqueue(self, command: FlowWorkCommand) -> FlowWorkItem: ...

    async def get(self, work_id: UUID) -> FlowWorkItem: ...

    async def claim(
        self,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowWorkItem | None: ...

    async def complete(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        now: datetime | None = None,
    ) -> FlowWorkItem: ...

    async def renew(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowWorkItem: ...

    async def block(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        reason_code: str,
        now: datetime | None = None,
    ) -> FlowWorkItem: ...

    async def retry(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        delay_seconds: float = 0,
        error_type: str | None = None,
        now: datetime | None = None,
    ) -> FlowWorkItem: ...


class FlowWorkReviewStore(Protocol):
    """Trusted operator port, separate from ordinary worker delivery access."""

    async def get(self, work_id: UUID) -> FlowWorkItem: ...

    async def list_blocked(self, *, limit: int = 100) -> tuple[FlowWorkItem, ...]: ...

    async def review(self, review: FlowWorkReview) -> FlowWorkReviewResult: ...

    async def list_reviews(self, work_id: UUID) -> tuple[FlowWorkReview, ...]: ...


class InMemoryFlowWorkSource:
    """Process-local work source with production-equivalent delivery semantics."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock
        self._items: dict[UUID, FlowWorkItem] = {}
        self._idempotency: dict[str, UUID] = {}
        self._reviews: dict[UUID, list[FlowWorkReview]] = {}
        self._review_idempotency: dict[str, FlowWorkReview] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, command: FlowWorkCommand) -> FlowWorkItem:
        async with self._lock:
            existing_id = self._idempotency.get(command.idempotency_key)
            if existing_id is not None:
                existing = self._items[existing_id]
                if not existing.command.has_same_intent(command):
                    raise FlowWorkIdempotencyConflictError(
                        f"Flow work idempotency key "
                        f"'{command.idempotency_key}' has different intent"
                    )
                return existing.model_copy(deep=True)
            now = self._clock()
            item = FlowWorkItem(
                command=command,
                available_at=now,
                updated_at=now,
            )
            self._items[item.id] = item
            self._idempotency[command.idempotency_key] = item.id
            return item.model_copy(deep=True)

    async def get(self, work_id: UUID) -> FlowWorkItem:
        async with self._lock:
            return self._copy_item(work_id)

    async def list_blocked(
        self,
        *,
        limit: int = 100,
    ) -> tuple[FlowWorkItem, ...]:
        _validate_list_limit(limit)
        async with self._lock:
            items = sorted(
                (
                    item
                    for item in self._items.values()
                    if item.status is FlowWorkStatus.BLOCKED
                ),
                key=lambda item: (item.updated_at, str(item.id)),
            )[:limit]
            return tuple(item.model_copy(deep=True) for item in items)

    async def review(self, review: FlowWorkReview) -> FlowWorkReviewResult:
        async with self._lock:
            existing = self._review_idempotency.get(review.idempotency_key)
            if existing is not None:
                _validate_review_intent(existing, review)
                return FlowWorkReviewResult(
                    review=existing.model_copy(deep=True),
                    item=self._copy_item(existing.work_id),
                )
            try:
                current = self._items[review.work_id]
            except KeyError as exc:
                raise FlowWorkNotFoundError(
                    f"Flow work item '{review.work_id}' was not found"
                ) from exc
            if current.status is not FlowWorkStatus.BLOCKED:
                raise FlowWorkReviewStateError(
                    f"Flow work item '{review.work_id}' is not blocked"
                )
            reviewed_at = self._clock()
            replacement = _apply_review(current, review, now=reviewed_at)
            persisted_review = review.model_copy(
                update={"created_at": reviewed_at}
            )
            self._items[current.id] = replacement
            self._reviews.setdefault(current.id, []).append(persisted_review)
            self._review_idempotency[review.idempotency_key] = persisted_review
            return FlowWorkReviewResult(
                review=persisted_review.model_copy(deep=True),
                item=replacement.model_copy(deep=True),
            )

    async def list_reviews(self, work_id: UUID) -> tuple[FlowWorkReview, ...]:
        async with self._lock:
            if work_id not in self._items:
                raise FlowWorkNotFoundError(
                    f"Flow work item '{work_id}' was not found"
                )
            return tuple(
                review.model_copy(deep=True)
                for review in self._reviews.get(work_id, ())
            )

    async def claim(
        self,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowWorkItem | None:
        _validate_delivery_arguments(owner_id, ttl_seconds)
        claimed_at = now or self._clock()
        async with self._lock:
            candidates = [
                item
                for item in self._items.values()
                if (
                    item.status is FlowWorkStatus.PENDING
                    and item.available_at <= claimed_at
                )
                or (
                    item.status is FlowWorkStatus.CLAIMED
                    and item.lease_expires_at is not None
                    and item.lease_expires_at <= claimed_at
                )
            ]
            if not candidates:
                return None
            current = min(
                candidates,
                key=lambda item: (
                    item.available_at,
                    item.command.created_at,
                    str(item.id),
                ),
            )
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
            self._items[current.id] = claimed
            return claimed.model_copy(deep=True)

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
        async with self._lock:
            current = self._require_active_delivery(
                work_id,
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
            self._items[work_id] = renewed
            return renewed.model_copy(deep=True)

    async def complete(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        now: datetime | None = None,
    ) -> FlowWorkItem:
        completed_at = now or self._clock()
        async with self._lock:
            current = self._require_delivery(
                work_id,
                delivery_token,
                now=completed_at,
                idempotent_status=FlowWorkStatus.COMPLETED,
            )
            if current.status is FlowWorkStatus.COMPLETED:
                return current.model_copy(deep=True)
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
            self._items[work_id] = completed
            return completed.model_copy(deep=True)

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
        async with self._lock:
            current = self._require_delivery(
                work_id,
                delivery_token,
                now=blocked_at,
                idempotent_status=FlowWorkStatus.BLOCKED,
            )
            if current.status is FlowWorkStatus.BLOCKED:
                return current.model_copy(deep=True)
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
            self._items[work_id] = blocked
            return blocked.model_copy(deep=True)

    async def retry(
        self,
        work_id: UUID,
        *,
        delivery_token: UUID,
        delay_seconds: float = 0,
        error_type: str | None = None,
        now: datetime | None = None,
    ) -> FlowWorkItem:
        if delay_seconds < 0:
            raise ValueError("Flow work retry delay_seconds must not be negative")
        if error_type is not None and (not error_type.strip() or len(error_type) > 256):
            raise ValueError("Flow work retry error_type must be 1 to 256 characters")
        retried_at = now or self._clock()
        async with self._lock:
            current = self._require_delivery(
                work_id,
                delivery_token,
                now=retried_at,
                idempotent_status=FlowWorkStatus.PENDING,
            )
            if current.status is FlowWorkStatus.PENDING:
                return current.model_copy(deep=True)
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
            self._items[work_id] = pending
            return pending.model_copy(deep=True)

    def _require_delivery(
        self,
        work_id: UUID,
        delivery_token: UUID,
        *,
        now: datetime,
        idempotent_status: FlowWorkStatus,
    ) -> FlowWorkItem:
        try:
            current = self._items[work_id]
        except KeyError as exc:
            raise FlowWorkNotFoundError(
                f"Flow work item '{work_id}' was not found"
            ) from exc
        if (
            current.status is idempotent_status
            and current.last_delivery_token == delivery_token
        ):
            return current
        if (
            current.status is not FlowWorkStatus.CLAIMED
            or current.delivery_token != delivery_token
            or current.lease_expires_at is None
            or current.lease_expires_at <= now
        ):
            raise FlowWorkDeliveryLostError(
                f"Flow work delivery for '{work_id}' was lost"
            )
        return current

    def _require_active_delivery(
        self,
        work_id: UUID,
        delivery_token: UUID,
        *,
        now: datetime,
    ) -> FlowWorkItem:
        try:
            current = self._items[work_id]
        except KeyError as exc:
            raise FlowWorkNotFoundError(
                f"Flow work item '{work_id}' was not found"
            ) from exc
        if (
            current.status is not FlowWorkStatus.CLAIMED
            or current.delivery_token != delivery_token
            or current.lease_expires_at is None
            or current.lease_expires_at <= now
        ):
            raise FlowWorkDeliveryLostError(
                f"Flow work delivery for '{work_id}' was lost"
            )
        return current

    def _copy_item(self, work_id: UUID) -> FlowWorkItem:
        try:
            return self._items[work_id].model_copy(deep=True)
        except KeyError as exc:
            raise FlowWorkNotFoundError(
                f"Flow work item '{work_id}' was not found"
            ) from exc


def _validate_delivery_arguments(owner_id: str, ttl_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Flow work owner_id must not be blank")
    if len(owner_id) > 256:
        raise ValueError("Flow work owner_id must not exceed 256 characters")
    if ttl_seconds <= 0:
        raise ValueError("Flow work ttl_seconds must be greater than zero")


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


_in_memory_work_source_contract: type[FlowWorkSource] = InMemoryFlowWorkSource
_in_memory_review_store_contract: type[FlowWorkReviewStore] = (
    InMemoryFlowWorkSource
)
