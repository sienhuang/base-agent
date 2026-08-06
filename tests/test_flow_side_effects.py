from uuid import uuid4

import pytest
from pydantic import ValidationError

from base_agent import (
    FlowSideEffect,
    FlowSideEffectConflictError,
    FlowSideEffectPhase,
    FlowSideEffectRetryMode,
    FlowSideEffectRevisionConflictError,
    InMemoryFlowSideEffectLedger,
    InvalidFlowSideEffectTransitionError,
)


def effect(
    *,
    flow_run_id=None,
    invocation_id=None,
    operation_name: str = "payments.charge",
    retry_mode: FlowSideEffectRetryMode = FlowSideEffectRetryMode.UNSAFE,
) -> FlowSideEffect:
    return FlowSideEffect(
        flow_run_id=flow_run_id or uuid4(),
        invocation_id=invocation_id or uuid4(),
        operation_key="tool-call-1",
        operation_name=operation_name,
        retry_mode=retry_mode,
        idempotency_key_digest=(
            "a" * 64
            if retry_mode is FlowSideEffectRetryMode.IDEMPOTENT
            else None
        ),
    )


def test_idempotent_effect_requires_only_a_digest_not_the_secret_key() -> None:
    with pytest.raises(ValidationError, match="idempotency key digest"):
        FlowSideEffect(
            flow_run_id=uuid4(),
            invocation_id=uuid4(),
            operation_key="tool-call-1",
            operation_name="payments.charge",
            retry_mode=FlowSideEffectRetryMode.IDEMPOTENT,
        )


@pytest.mark.asyncio
async def test_prepare_is_idempotent_by_invocation_operation_key() -> None:
    ledger = InMemoryFlowSideEffectLedger()
    original = effect()
    prepared = await ledger.prepare(original)
    duplicate = await ledger.prepare(original.model_copy(update={"id": uuid4()}))

    assert duplicate == prepared
    assert await ledger.list_for_invocation(original.invocation_id) == (prepared,)

    with pytest.raises(FlowSideEffectConflictError, match="different intent"):
        await ledger.prepare(
            original.model_copy(
                update={
                    "id": uuid4(),
                    "operation_name": "payments.refund",
                }
            )
        )


@pytest.mark.asyncio
async def test_side_effect_lifecycle_is_fenced_and_never_guesses_failure() -> None:
    ledger = InMemoryFlowSideEffectLedger()
    prepared = await ledger.prepare(effect())
    started = await ledger.mark_started(
        prepared.id,
        expected_revision=prepared.revision,
    )

    assert started.phase is FlowSideEffectPhase.STARTED
    assert not started.retry_safe
    with pytest.raises(FlowSideEffectRevisionConflictError):
        await ledger.confirm(prepared.id, expected_revision=prepared.revision)
    with pytest.raises(InvalidFlowSideEffectTransitionError):
        await ledger.abort(started.id, expected_revision=started.revision)

    confirmed = await ledger.confirm(
        started.id,
        expected_revision=started.revision,
    )
    assert confirmed.phase is FlowSideEffectPhase.CONFIRMED
    assert confirmed.revision == 3
    assert not confirmed.retry_safe


@pytest.mark.asyncio
async def test_prepared_or_downstream_idempotent_effect_is_retry_safe() -> None:
    ledger = InMemoryFlowSideEffectLedger()
    prepared_unsafe = await ledger.prepare(effect())
    prepared_idempotent = await ledger.prepare(
        effect(retry_mode=FlowSideEffectRetryMode.IDEMPOTENT)
    )
    started_idempotent = await ledger.mark_started(
        prepared_idempotent.id,
        expected_revision=prepared_idempotent.revision,
    )

    assert prepared_unsafe.retry_safe
    assert started_idempotent.retry_safe


@pytest.mark.asyncio
async def test_known_aborted_effect_can_start_on_a_later_attempt() -> None:
    ledger = InMemoryFlowSideEffectLedger()
    prepared = await ledger.prepare(effect())
    aborted = await ledger.abort(
        prepared.id,
        expected_revision=prepared.revision,
    )
    restarted = await ledger.mark_started(
        aborted.id,
        expected_revision=aborted.revision,
    )

    assert restarted.phase is FlowSideEffectPhase.STARTED
