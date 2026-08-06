import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from base_agent import (
    FlowWorkCommand,
    FlowWorkDeliveryLostError,
    FlowWorkIdempotencyConflictError,
    FlowWorkItem,
    FlowWorkKind,
    FlowWorkReview,
    FlowWorkReviewConflictError,
    FlowWorkReviewDecision,
    FlowWorkReviewStateError,
    FlowWorkStatus,
    InMemoryFlowWorkSource,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_for_the_same_intent() -> None:
    source = InMemoryFlowWorkSource()
    run_id = uuid4()
    first, second = await asyncio.gather(
        source.enqueue(
            FlowWorkCommand(
                run_id=run_id,
                idempotency_key="run:1",
                data={"request_id": "request-1"},
            )
        ),
        source.enqueue(
            FlowWorkCommand(
                run_id=run_id,
                idempotency_key="run:1",
                data={"request_id": "request-1"},
            )
        ),
    )

    assert first == second
    assert first.status is FlowWorkStatus.PENDING


@pytest.mark.asyncio
async def test_reusing_idempotency_key_for_different_intent_is_rejected() -> None:
    source = InMemoryFlowWorkSource()
    await source.enqueue(
        FlowWorkCommand(
            run_id=uuid4(),
            idempotency_key="shared-key",
        )
    )

    with pytest.raises(FlowWorkIdempotencyConflictError, match="different intent"):
        await source.enqueue(
            FlowWorkCommand(
                run_id=uuid4(),
                idempotency_key="shared-key",
                kind=FlowWorkKind.CANCEL,
            )
        )


@pytest.mark.asyncio
async def test_only_one_concurrent_delivery_claims_an_item() -> None:
    source = InMemoryFlowWorkSource()
    queued = await source.enqueue(
        FlowWorkCommand(run_id=uuid4(), idempotency_key="claim-once")
    )

    claims = await asyncio.gather(
        source.claim(owner_id="worker-a", ttl_seconds=10),
        source.claim(owner_id="worker-b", ttl_seconds=10),
    )

    claimed = next(item for item in claims if item is not None)
    assert claimed.id == queued.id
    assert claimed.attempt == 1
    assert sum(item is None for item in claims) == 1


@pytest.mark.asyncio
async def test_expired_delivery_is_redelivered_and_old_token_is_fenced() -> None:
    clock = MutableClock()
    source = InMemoryFlowWorkSource(clock=clock)
    queued = await source.enqueue(
        FlowWorkCommand(run_id=uuid4(), idempotency_key="redelivery")
    )
    first = await source.claim(owner_id="worker-a", ttl_seconds=10)
    assert first is not None
    assert first.delivery_token is not None

    clock.advance(11)
    second = await source.claim(owner_id="worker-b", ttl_seconds=10)
    assert second is not None
    assert second.id == first.id
    assert second.attempt == 2
    assert second.delivery_token != first.delivery_token

    with pytest.raises(FlowWorkDeliveryLostError):
        await source.complete(
            queued.id,
            delivery_token=first.delivery_token,
        )
    completed = await source.complete(
        queued.id,
        delivery_token=second.delivery_token,
    )
    repeated = await source.complete(
        queued.id,
        delivery_token=second.delivery_token,
    )
    assert completed == repeated
    assert completed.status is FlowWorkStatus.COMPLETED


@pytest.mark.asyncio
async def test_retry_delay_and_settlement_are_idempotent() -> None:
    clock = MutableClock()
    source = InMemoryFlowWorkSource(clock=clock)
    queued = await source.enqueue(
        FlowWorkCommand(run_id=uuid4(), idempotency_key="retry")
    )
    claimed = await source.claim(owner_id="worker-a", ttl_seconds=10)
    assert claimed is not None
    assert claimed.delivery_token is not None

    pending = await source.retry(
        queued.id,
        delivery_token=claimed.delivery_token,
        delay_seconds=5,
        error_type="TemporaryError",
    )
    repeated = await source.retry(
        queued.id,
        delivery_token=claimed.delivery_token,
        delay_seconds=5,
        error_type="TemporaryError",
    )
    assert pending == repeated
    assert pending.status is FlowWorkStatus.PENDING
    assert pending.last_error_type == "TemporaryError"
    assert await source.claim(owner_id="worker-b", ttl_seconds=10) is None

    clock.advance(5)
    redelivered = await source.claim(owner_id="worker-b", ttl_seconds=10)
    assert redelivered is not None
    assert redelivered.attempt == 2


@pytest.mark.asyncio
async def test_delivery_renewal_preserves_attempt_and_fencing_token() -> None:
    clock = MutableClock()
    source = InMemoryFlowWorkSource(clock=clock)
    await source.enqueue(
        FlowWorkCommand(run_id=uuid4(), idempotency_key="renew")
    )
    claimed = await source.claim(owner_id="worker-a", ttl_seconds=10)
    assert claimed is not None
    assert claimed.delivery_token is not None

    clock.advance(5)
    renewed = await source.renew(
        claimed.id,
        delivery_token=claimed.delivery_token,
        ttl_seconds=10,
    )
    assert renewed.attempt == claimed.attempt
    assert renewed.delivery_token == claimed.delivery_token
    assert renewed.lease_expires_at == clock() + timedelta(seconds=10)

    clock.advance(6)
    assert await source.claim(owner_id="worker-b", ttl_seconds=10) is None


@pytest.mark.asyncio
async def test_block_is_idempotent_and_removes_item_from_delivery() -> None:
    source = InMemoryFlowWorkSource()
    queued = await source.enqueue(
        FlowWorkCommand(run_id=uuid4(), idempotency_key="manual-review")
    )
    claimed = await source.claim(owner_id="worker-a", ttl_seconds=10)
    assert claimed is not None
    assert claimed.delivery_token is not None

    blocked = await source.block(
        queued.id,
        delivery_token=claimed.delivery_token,
        reason_code="active_invocation_uncertain",
    )
    repeated = await source.block(
        queued.id,
        delivery_token=claimed.delivery_token,
        reason_code="active_invocation_uncertain",
    )
    assert blocked == repeated
    assert blocked.status is FlowWorkStatus.BLOCKED
    assert blocked.blocked_reason == "active_invocation_uncertain"
    assert await source.claim(owner_id="worker-b", ttl_seconds=10) is None


async def create_blocked_item(
    source: InMemoryFlowWorkSource,
    *,
    key: str,
) -> FlowWorkItem:
    queued = await source.enqueue(
        FlowWorkCommand(run_id=uuid4(), idempotency_key=key)
    )
    claimed = await source.claim(owner_id="worker-a", ttl_seconds=10)
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.delivery_token is not None
    return await source.block(
        queued.id,
        delivery_token=claimed.delivery_token,
        reason_code="active_invocation_uncertain",
    )


@pytest.mark.asyncio
async def test_operator_can_list_and_approve_blocked_work_for_delayed_retry() -> None:
    clock = MutableClock()
    source = InMemoryFlowWorkSource(clock=clock)
    blocked = await create_blocked_item(source, key="approve-review")
    assert await source.list_blocked() == (blocked,)
    review = FlowWorkReview(
        work_id=blocked.id,
        decision=FlowWorkReviewDecision.APPROVE_RETRY,
        reviewer_id="operator-1",
        reason_code="downstream_idempotency_verified",
        idempotency_key="review:approve:1",
        delay_seconds=5,
    )

    first = await source.review(review)
    repeated = await source.review(review.model_copy(update={"id": uuid4()}))

    assert first == repeated
    assert first.item.status is FlowWorkStatus.PENDING
    assert first.item.blocked_reason is None
    assert first.item.available_at == clock() + timedelta(seconds=5)
    assert await source.list_blocked() == ()
    assert await source.list_reviews(blocked.id) == (first.review,)
    assert await source.claim(owner_id="worker-b", ttl_seconds=10) is None
    clock.advance(5)
    assert await source.claim(owner_id="worker-b", ttl_seconds=10) is not None


@pytest.mark.asyncio
async def test_operator_can_reject_and_archive_blocked_work() -> None:
    source = InMemoryFlowWorkSource()
    blocked = await create_blocked_item(source, key="reject-review")
    result = await source.review(
        FlowWorkReview(
            work_id=blocked.id,
            decision=FlowWorkReviewDecision.REJECT,
            reviewer_id="operator-2",
            reason_code="unsafe_side_effect_unknown",
            idempotency_key="review:reject:1",
        )
    )

    assert result.item.status is FlowWorkStatus.DISCARDED
    assert result.item.blocked_reason is None
    assert await source.claim(owner_id="worker-b", ttl_seconds=10) is None
    assert await source.list_reviews(blocked.id) == (result.review,)


@pytest.mark.asyncio
async def test_review_idempotency_conflict_and_nonblocked_review_are_rejected() -> None:
    source = InMemoryFlowWorkSource()
    blocked = await create_blocked_item(source, key="review-conflict")
    approved = FlowWorkReview(
        work_id=blocked.id,
        decision=FlowWorkReviewDecision.APPROVE_RETRY,
        reviewer_id="operator-1",
        reason_code="safe",
        idempotency_key="review:shared",
    )
    await source.review(approved)

    with pytest.raises(FlowWorkReviewConflictError, match="different intent"):
        await source.review(
            approved.model_copy(
                update={
                    "id": uuid4(),
                    "decision": FlowWorkReviewDecision.REJECT,
                    "reason_code": "reject",
                }
            )
        )
    with pytest.raises(FlowWorkReviewStateError, match="not blocked"):
        await source.review(
            FlowWorkReview(
                work_id=blocked.id,
                decision=FlowWorkReviewDecision.REJECT,
                reviewer_id="operator-2",
                reason_code="late_review",
                idempotency_key="review:late",
            )
        )


@pytest.mark.asyncio
async def test_concurrent_operator_decisions_commit_exactly_one_audit_record() -> None:
    source = InMemoryFlowWorkSource()
    blocked = await create_blocked_item(source, key="concurrent-review")
    decisions = await asyncio.gather(
        source.review(
            FlowWorkReview(
                work_id=blocked.id,
                decision=FlowWorkReviewDecision.APPROVE_RETRY,
                reviewer_id="operator-a",
                reason_code="approve",
                idempotency_key="review:concurrent:a",
            )
        ),
        source.review(
            FlowWorkReview(
                work_id=blocked.id,
                decision=FlowWorkReviewDecision.REJECT,
                reviewer_id="operator-b",
                reason_code="reject",
                idempotency_key="review:concurrent:b",
            )
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in decisions) == 1
    assert sum(
        isinstance(result, FlowWorkReviewStateError) for result in decisions
    ) == 1
    assert len(await source.list_reviews(blocked.id)) == 1
