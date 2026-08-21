from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from base_agent import Agent, AgentDefinition
from base_agent.events import InMemoryEventStore
from base_agent.execution import (
    ExecutionAttemptStatus,
    ExecutionLeaseLostError,
    ExecutionOperation,
    InMemoryExecutionStore,
    RecoveryDecision,
    RecoveryStrategy,
)
from base_agent.models import RunStatus
from base_agent.run_store import AgentRunRecord, InMemoryRunStore
from base_agent.server import ExecutionWorker, FencedEventStore, create_http_app
from base_agent.testing import FakeModel


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 20, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _record(run_id: str = "run-1") -> AgentRunRecord:
    return AgentRunRecord(
        run_id=run_id,
        agent_id="queued-agent",
        agent_version="1.0.0",
        snapshot_fingerprint="snapshot",
        runtime_build_id="build-1",
    )


async def test_attempt_lease_expiry_requires_an_explicit_recovery_decision() -> None:
    clock = MutableClock()
    runs = InMemoryRunStore(clock=clock)
    executions = InMemoryExecutionStore(runs, clock=clock)
    queued, request = await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-1",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert queued.status is RunStatus.QUEUED
    assert request.status.value == "pending"
    assert claimed is not None

    clock.advance(31)
    transitions = await executions.reconcile_expired()
    recovery = await runs.get("run-1")
    latest = await executions.latest_attempt("run-1")

    assert len(transitions) == 1
    assert recovery.status is RunStatus.RECOVERY_REQUIRED
    assert recovery.recovery_reason_code == "owner_lease_expired"
    assert latest is not None
    assert latest.status is ExecutionAttemptStatus.LEASE_EXPIRED
    with pytest.raises(ExecutionLeaseLostError):
        await executions.settle(claimed.lease, status=RunStatus.SUCCEEDED)

    retried, decision = await executions.decide_recovery(
        "run-1",
        decision=RecoveryDecision.RETRY,
        strategy=RecoveryStrategy.RESTART,
        subject_id="operator-1",
        reason_code="verified_safe",
        expected_revision=recovery.revision,
        idempotency_key="ticket-1",
    )
    repeated, repeated_decision = await executions.decide_recovery(
        "run-1",
        decision=RecoveryDecision.RETRY,
        strategy=RecoveryStrategy.RESTART,
        subject_id="operator-1",
        reason_code="verified_safe",
        expected_revision=recovery.revision,
        idempotency_key="ticket-1",
    )
    assert retried.status is RunStatus.QUEUED
    assert repeated == retried
    assert repeated_decision == decision


async def test_expired_attempt_cannot_be_marked_started() -> None:
    clock = MutableClock()
    runs = InMemoryRunStore(clock=clock)
    executions = InMemoryExecutionStore(runs, clock=clock)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-expired-start",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert claimed is not None

    clock.advance(31)

    with pytest.raises(ExecutionLeaseLostError):
        await executions.mark_started(claimed.lease, child_pid=123)


async def test_existing_run_get_includes_recovery_projection_and_decision_post() -> None:
    clock = MutableClock()
    events = InMemoryEventStore()
    runs = InMemoryRunStore(clock=clock)
    executions = InMemoryExecutionStore(runs, clock=clock)
    agent = Agent(
        AgentDefinition(
            id="queued-agent",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("unused"),
        event_store=events,
    )
    app = create_http_app(
        agent,
        event_store=events,
        run_store=runs,
        execution_store=executions,
        execution_mode="queued",
        runtime_build_id="build-1",
        operator_authorizer=lambda request, subject_id: None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        started = await client.post("/v1/runs", json={"prompt": "work"})
        run_id = started.json()["run_id"]
        claimed = await executions.claim_next(
            owner_id="worker-1",
            runtime_route="default",
            runtime_build_id="build-1",
            lease_seconds=30,
        )
        assert claimed is not None
        clock.advance(31)
        await executions.reconcile_expired()

        response = await client.get(f"/v1/runs/{run_id}")
        payload = response.json()
        assert payload["status"] == "recovery_required"
        assert payload["recovery"]["reason_code"] == "owner_lease_expired"
        assert payload["recovery"]["last_attempt"]["status"] == "lease_expired"

        decided = await client.post(
            f"/v1/operator/runs/{run_id}/recovery-decisions",
            json={
                "decision": "retry",
                "strategy": "restart",
                "expected_revision": payload["recovery"]["revision"],
                "subject_id": "operator-1",
                "reason_code": "verified_safe",
                "idempotency_key": "ticket-2",
            },
        )
        assert decided.status_code == 200
        assert decided.json()["status"] == "queued"
        assert "recovery" not in decided.json()


async def test_queued_submission_uses_the_http_idempotency_key() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)
    agent = Agent(
        AgentDefinition(id="queued-agent", version="1.0.0", instructions="Answer."),
        model=FakeModel.text("unused"),
        event_store=events,
    )
    app = create_http_app(
        agent,
        event_store=events,
        run_store=runs,
        execution_store=executions,
        execution_mode="queued",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "submit-1"},
            json={"prompt": "work"},
        )
        repeated = await client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "submit-1"},
            json={"prompt": "work"},
        )
        conflicting = await client.post(
            "/v1/runs",
            headers={"Idempotency-Key": "submit-1"},
            json={"prompt": "different work"},
        )

    assert repeated.json() == first.json()
    assert conflicting.status_code == 409


async def test_worker_heartbeats_while_a_child_process_is_blocked() -> None:
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-worker",
    )
    script = (
        "import json,sys,time; "
        "json.load(sys.stdin); time.sleep(0.25); "
        "print(json.dumps({'status':'succeeded','output':'ok','text':'ok',"
        "'error':None,'pending_input':None,'checkpoint_revision':None}))"
    )
    worker = ExecutionWorker(
        executions,
        child_command=(sys.executable, "-c", script),
        runtime_build_id="build-1",
        lease_seconds=0.12,
        heartbeat_interval_seconds=0.03,
        poll_interval_seconds=0.01,
        reconcile_interval_seconds=0.05,
    )

    assert await worker.run_once() is True
    record = await runs.get("run-1")
    attempt = await executions.latest_attempt("run-1")
    assert record.status is RunStatus.SUCCEEDED
    assert record.output == "ok"
    assert attempt is not None
    assert attempt.status is ExecutionAttemptStatus.SUCCEEDED
    assert attempt.heartbeat_at > attempt.claimed_at


async def test_child_terminal_event_is_not_authoritative_before_settlement() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs, event_store=events)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-fenced-events",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert claimed is not None
    child_events = FencedEventStore(events, executions, claimed.lease)

    child_terminal = await child_events.emit("run-1", "run.succeeded", {"text": "ok"})
    before_settlement = await events.list("run-1")
    await executions.settle(
        claimed.lease,
        status=RunStatus.SUCCEEDED,
        output="ok",
        text="ok",
    )
    after_settlement = await events.list("run-1")

    assert child_terminal.kind == "execution.child.succeeded"
    assert "run.succeeded" not in {event.kind for event in before_settlement}
    assert [event.kind for event in after_settlement].count("run.succeeded") == 1


async def test_incompatible_runtime_blocks_instead_of_silently_replaying() -> None:
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-blocked",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="wrong-build",
        lease_seconds=30,
    )
    assert claimed is not None

    blocked = await executions.block(
        claimed.lease,
        reason_code="runtime_unavailable",
        error="snapshot mismatch",
    )
    attempt = await executions.latest_attempt("run-1")
    assert blocked.status is RunStatus.BLOCKED
    assert blocked.recovery_reason_code == "runtime_unavailable"
    assert attempt is not None
    assert attempt.status is ExecutionAttemptStatus.FAILED


async def test_worker_shutdown_marks_active_attempt_for_recovery() -> None:
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-shutdown",
    )
    script = "import json,sys,time; json.load(sys.stdin); time.sleep(10)"
    worker = ExecutionWorker(
        executions,
        child_command=(sys.executable, "-c", script),
        runtime_build_id="build-1",
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
        poll_interval_seconds=0.01,
        reconcile_interval_seconds=0.1,
    )
    task = asyncio.create_task(worker.run_once())
    for _ in range(100):
        attempt = await executions.latest_attempt("run-1")
        if attempt is not None and attempt.status is ExecutionAttemptStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("Worker did not start its child")

    await worker.close()
    await task
    record = await runs.get("run-1")
    attempt = await executions.latest_attempt("run-1")
    assert record.status is RunStatus.RECOVERY_REQUIRED
    assert record.recovery_reason_code == "worker_shutdown"
    assert attempt is not None
    assert attempt.status is ExecutionAttemptStatus.INTERRUPTED
