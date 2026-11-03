"""Demonstrate fenced execution and evidence-gated recovery.

Run from the repository root:

    .venv/bin/python examples/recovery_safety_demo.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from base_agent.events import InMemoryEventStore
from base_agent.execution import (
    ExecutionLeaseLostError,
    ExecutionOperation,
    ExecutionRequestConflictError,
    InMemoryExecutionStore,
    RecoveryDecision,
    RecoveryGateBlockedError,
    RecoveryStrategy,
)
from base_agent.run_store import AgentRunRecord, InMemoryRunStore
from base_agent.server import FencedEventStore


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def run_record(run_id: str) -> AgentRunRecord:
    return AgentRunRecord(
        run_id=run_id,
        agent_id="recovery-demo",
        agent_version="1.0.0",
        snapshot_fingerprint="snapshot-v1",
        runtime_build_id="build-v1",
    )


def tool_started_payload(
    *, operation_id: str, side_effect: str, intent_sha256: str = "a" * 64
) -> dict[str, object]:
    return {
        "tool_name": "external_operation",
        "tool_call_id": "call-1",
        "side_effect": side_effect,
        "operation_id": operation_id,
        "idempotency_key": operation_id,
        "intent_sha256": intent_sha256,
    }


def short(value: str) -> str:
    return value[:8]


async def mutating_operation_is_blocked() -> None:
    print("\nScenario A: an uncheckpointed mutating operation")
    clock = MutableClock()
    events = InMemoryEventStore()
    runs = InMemoryRunStore(clock=clock)
    executions = InMemoryExecutionStore(runs, event_store=events, clock=clock)

    await executions.enqueue_run(
        run_record("run-mutating"),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "charge the customer"},
        idempotency_key="submit-mutating",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-v1",
        lease_seconds=30,
    )
    assert claimed is not None
    attempt_events = FencedEventStore(events, executions, claimed.lease)
    payload = tool_started_payload(
        operation_id="payment:order-42",
        side_effect="mutating",
    )
    await attempt_events.emit("run-mutating", "tool.started", payload)
    print(
        f"1. attempt={short(claimed.attempt.attempt_id)} acquired; "
        "mutating tool.started committed"
    )

    clock.advance(31)
    await executions.reconcile_expired()
    recovered = await runs.get("run-mutating")
    evidence = await executions.recovery_evidence("run-mutating")
    blocker = evidence.blockers_for(RecoveryStrategy.RESTART)[0]
    print(
        f"2. lease expired -> run={recovered.status.value}; "
        f"side_effect={evidence.side_effect_evidence[0].status.value}"
    )
    print(f"3. restart blocker={blocker.code} operation={blocker.operation_id}")

    try:
        await attempt_events.emit(
            "run-mutating",
            "tool.completed",
            {"operation_id": "payment:order-42", "status": "success"},
        )
    except ExecutionLeaseLostError:
        print("4. stale child completion rejected by fencing")

    try:
        await executions.decide_recovery(
            "run-mutating",
            decision=RecoveryDecision.RETRY,
            strategy=RecoveryStrategy.RESTART,
            subject_id="operator-1",
            reason_code="retry_requested",
            expected_revision=recovered.revision,
            idempotency_key="ticket-mutating",
        )
    except RecoveryGateBlockedError as exc:
        print(f"5. automatic replay denied: {exc}")


async def idempotent_operation_can_retry() -> None:
    print("\nScenario B: a stable idempotent operation")
    clock = MutableClock()
    events = InMemoryEventStore()
    runs = InMemoryRunStore(clock=clock)
    executions = InMemoryExecutionStore(runs, event_store=events, clock=clock)

    await executions.enqueue_run(
        run_record("run-idempotent"),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "upsert the report"},
        idempotency_key="submit-idempotent",
    )
    first = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-v1",
        lease_seconds=30,
    )
    assert first is not None
    first_events = FencedEventStore(events, executions, first.lease)
    payload = tool_started_payload(
        operation_id="report:2026-08-26",
        side_effect="idempotent",
    )
    await first_events.emit("run-idempotent", "tool.started", payload)
    print(f"1. attempt-1={short(first.attempt.attempt_id)} started idempotent intent")

    clock.advance(31)
    await executions.reconcile_expired()
    recovered = await runs.get("run-idempotent")
    blockers = await executions.recovery_evidence("run-idempotent")
    print(
        "2. restart blockers="
        f"{len(blockers.blockers_for(RecoveryStrategy.RESTART))}"
    )

    retried, _ = await executions.decide_recovery(
        "run-idempotent",
        decision=RecoveryDecision.RETRY,
        strategy=RecoveryStrategy.RESTART,
        subject_id="operator-1",
        reason_code="stable_operation_identity",
        expected_revision=recovered.revision,
        idempotency_key="ticket-idempotent",
    )
    second = await executions.claim_next(
        owner_id="worker-2",
        runtime_route="default",
        runtime_build_id="build-v1",
        lease_seconds=30,
    )
    assert second is not None
    second_events = FencedEventStore(events, executions, second.lease)
    await second_events.emit("run-idempotent", "tool.started", payload)
    print(
        f"3. operator retry -> {retried.status.value}; "
        f"attempt-2={short(second.attempt.attempt_id)} accepted same intent"
    )

    try:
        await second_events.emit(
            "run-idempotent",
            "tool.started",
            tool_started_payload(
                operation_id="report:2026-08-26",
                side_effect="idempotent",
                intent_sha256="c" * 64,
            ),
        )
    except ExecutionRequestConflictError:
        print("4. same operation_id with changed intent rejected")


async def main() -> None:
    await mutating_operation_is_blocked()
    await idempotent_operation_can_retry()


if __name__ == "__main__":
    asyncio.run(main())
