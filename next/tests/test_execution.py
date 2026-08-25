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
    ExecutionRequestConflictError,
    InMemoryExecutionStore,
    RecoveryDecision,
    RecoveryGateBlockedError,
    RecoveryStrategy,
    SideEffectStatus,
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


def _tool_evidence_payload(
    *,
    operation_id: str,
    side_effect: str,
    intent_sha256: str = "a" * 64,
) -> dict[str, object]:
    return {
        "tool_name": "external_operation",
        "tool_call_id": "call-1",
        "side_effect": side_effect,
        "operation_id": operation_id,
        "idempotency_key": operation_id,
        "intent_sha256": intent_sha256,
    }


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


async def test_uncheckpointed_mutating_evidence_blocks_recovery_replay() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs, event_store=events)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-mutating",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert claimed is not None
    child_events = FencedEventStore(events, executions, claimed.lease)
    await child_events.emit(
        "run-1",
        "tool.started",
        _tool_evidence_payload(
            operation_id="op:mutating",
            side_effect="mutating",
        ),
    )

    recovery = await executions.interrupt(
        claimed.lease,
        reason_code="worker_shutdown",
    )
    evidence = await executions.recovery_evidence("run-1")

    assert evidence.side_effect_evidence[0].status is SideEffectStatus.UNKNOWN
    assert [
        blocker.code
        for blocker in evidence.blockers_for(RecoveryStrategy.RESTART)
    ] == ["unsafe_side_effect_replay"]
    assert evidence.blockers_for(RecoveryStrategy.RESTART)[0].operation_id == (
        "op:mutating"
    )
    with pytest.raises(RecoveryGateBlockedError, match="unsafe_side_effect_replay"):
        await executions.decide_recovery(
            "run-1",
            decision=RecoveryDecision.RETRY,
            strategy=RecoveryStrategy.RESTART,
            subject_id="operator-1",
            reason_code="retry_requested",
            expected_revision=recovery.revision,
            idempotency_key="ticket-mutating",
        )
    with pytest.raises(ExecutionLeaseLostError):
        await child_events.emit(
            "run-1",
            "artifact.created",
            {
                "artifact_id": "late-artifact",
                "name": "late.txt",
                "media_type": "text/plain",
                "size_bytes": 4,
                "sha256": "b" * 64,
                "producer_run_id": "run-1",
                "producer_agent_id": "queued-agent",
            },
        )


async def test_idempotent_evidence_allows_retry_but_changed_intent_is_rejected() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs, event_store=events)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-idempotent",
    )
    first = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert first is not None
    first_events = FencedEventStore(events, executions, first.lease)
    payload = _tool_evidence_payload(
        operation_id="op:idempotent",
        side_effect="idempotent",
    )
    await first_events.emit("run-1", "tool.started", payload)
    recovery = await executions.interrupt(first.lease, reason_code="worker_shutdown")

    retried, _ = await executions.decide_recovery(
        "run-1",
        decision=RecoveryDecision.RETRY,
        strategy=RecoveryStrategy.RESTART,
        subject_id="operator-1",
        reason_code="stable_idempotency_key",
        expected_revision=recovery.revision,
        idempotency_key="ticket-idempotent",
    )
    assert retried.status is RunStatus.QUEUED
    second = await executions.claim_next(
        owner_id="worker-2",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert second is not None
    second_events = FencedEventStore(events, executions, second.lease)
    await second_events.emit("run-1", "tool.started", payload)
    with pytest.raises(ExecutionRequestConflictError, match="changed intent"):
        await second_events.emit(
            "run-1",
            "tool.started",
            _tool_evidence_payload(
                operation_id="op:idempotent",
                side_effect="idempotent",
                intent_sha256="c" * 64,
            ),
        )


async def test_checkpointed_waiting_operation_can_complete_under_continuation_attempt() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs, event_store=events)
    await executions.enqueue_run(
        _record(),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="submit-waiting-operation",
    )
    first = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert first is not None
    first_events = FencedEventStore(events, executions, first.lease)
    started = _tool_evidence_payload(
        operation_id="op:waiting",
        side_effect="mutating",
    )
    await first_events.emit("run-1", "tool.started", started)
    await first_events.emit(
        "run-1",
        "tool.waiting",
        {**started, "request_id": "input-1"},
    )
    await first_events.emit(
        "run-1",
        "run.waiting",
        {"checkpoint_revision": 1},
    )
    await executions.settle(
        first.lease,
        status=RunStatus.WAITING,
        pending_input={
            "request_id": "input-1",
            "kind": "tool_input",
            "tool_call_id": "call-1",
            "tool_name": "external_operation",
            "prompt": "Continue?",
            "metadata": {},
        },
        checkpoint_revision=1,
    )
    await executions.enqueue_continuation(
        "run-1",
        operation=ExecutionOperation.RESUME,
        payload={"input": "yes"},
        idempotency_key="resume-waiting-operation",
    )
    second = await executions.claim_next(
        owner_id="worker-2",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
    )
    assert second is not None
    second_events = FencedEventStore(events, executions, second.lease)
    await second_events.emit(
        "run-1",
        "tool.completed",
        {"operation_id": "op:waiting", "status": "success"},
    )

    evidence = await executions.recovery_evidence("run-1")
    assert evidence.side_effect_evidence[0].status is SideEffectStatus.SUCCEEDED
    assert evidence.side_effect_evidence[0].checkpoint_revision == 1
    assert evidence.side_effect_evidence[0].resolved_by_attempt_id == second.attempt.attempt_id


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


async def test_recovery_projection_exposes_evidence_and_strategy_blockers() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs, event_store=events)
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
        fenced = FencedEventStore(events, executions, claimed.lease)
        await fenced.emit(
            run_id,
            "tool.started",
            _tool_evidence_payload(
                operation_id="op:http-mutating",
                side_effect="mutating",
            ),
        )
        await executions.interrupt(claimed.lease, reason_code="worker_shutdown")

        response = await client.get(f"/v1/runs/{run_id}")
        recovery = response.json()["recovery"]

        assert recovery["side_effect_evidence"][0]["status"] == "unknown"
        assert recovery["strategy_blockers"]["restart"][0]["code"] == (
            "unsafe_side_effect_replay"
        )
        assert recovery["strategy_blockers"]["restart"][0]["operation_id"] == (
            "op:http-mutating"
        )
        assert "restart" not in recovery["allowed_strategies"]


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


async def test_worker_supervises_attempts_up_to_its_concurrency_limit() -> None:
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)
    run_ids = tuple(f"concurrent-run-{index}" for index in range(3))
    for run_id in run_ids:
        await executions.enqueue_run(
            _record(run_id),
            operation=ExecutionOperation.EXECUTE,
            payload={"prompt": run_id},
            idempotency_key=f"submit-{run_id}",
        )
    script = (
        "import json,sys,time; "
        "json.load(sys.stdin); time.sleep(0.25); "
        "print(json.dumps({'status':'succeeded','output':'ok','text':'ok'}))"
    )
    worker = ExecutionWorker(
        executions,
        child_command=(sys.executable, "-c", script),
        runtime_build_id="build-1",
        lease_seconds=0.15,
        heartbeat_interval_seconds=0.03,
        poll_interval_seconds=0.005,
        reconcile_interval_seconds=0.05,
        max_concurrent_attempts=2,
    )
    task = asyncio.create_task(worker.run_forever())
    try:
        for _ in range(200):
            if worker.active_attempt_count == 2:
                records = [await runs.get(run_id) for run_id in run_ids]
                if sum(record.status is RunStatus.RUNNING for record in records) == 2:
                    break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("Worker did not fill both execution slots")

        records = [await runs.get(run_id) for run_id in run_ids]
        assert sum(record.status is RunStatus.RUNNING for record in records) == 2
        assert sum(record.status is RunStatus.QUEUED for record in records) == 1

        for _ in range(300):
            records = [await runs.get(run_id) for run_id in run_ids]
            if all(record.status is RunStatus.SUCCEEDED for record in records):
                break
            await asyncio.sleep(0.005)
        else:
            raise AssertionError("Worker did not drain all queued executions")

        attempts = [await executions.latest_attempt(run_id) for run_id in run_ids]
        assert all(attempt is not None for attempt in attempts)
        assert all(
            attempt.heartbeat_at > attempt.claimed_at
            for attempt in attempts
            if attempt is not None
        )
    finally:
        await worker.close()
        await task


async def test_worker_shutdown_interrupts_every_active_attempt() -> None:
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)
    run_ids = tuple(f"shutdown-run-{index}" for index in range(3))
    for run_id in run_ids:
        await executions.enqueue_run(
            _record(run_id),
            operation=ExecutionOperation.EXECUTE,
            payload={"prompt": run_id},
            idempotency_key=f"submit-{run_id}",
        )
    script = "import json,sys,time; json.load(sys.stdin); time.sleep(10)"
    worker = ExecutionWorker(
        executions,
        child_command=(sys.executable, "-c", script),
        runtime_build_id="build-1",
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
        poll_interval_seconds=0.005,
        reconcile_interval_seconds=0.1,
        max_concurrent_attempts=3,
    )
    task = asyncio.create_task(worker.run_forever())
    for _ in range(200):
        if worker.active_attempt_count == 3:
            break
        await asyncio.sleep(0.005)
    else:
        await worker.close()
        await task
        raise AssertionError("Worker did not start all concurrent children")

    await worker.close()
    await task

    records = [await runs.get(run_id) for run_id in run_ids]
    attempts = [await executions.latest_attempt(run_id) for run_id in run_ids]
    assert all(record.status is RunStatus.RECOVERY_REQUIRED for record in records)
    assert all(record.recovery_reason_code == "worker_shutdown" for record in records)
    assert all(
        attempt is not None
        and attempt.status is ExecutionAttemptStatus.INTERRUPTED
        for attempt in attempts
    )


def test_worker_rejects_non_positive_concurrency() -> None:
    runs = InMemoryRunStore()
    executions = InMemoryExecutionStore(runs)

    with pytest.raises(ValueError, match="max_concurrent_attempts"):
        ExecutionWorker(
            executions,
            child_command=(sys.executable, "-c", "pass"),
            runtime_build_id="build-1",
            max_concurrent_attempts=0,
        )


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
