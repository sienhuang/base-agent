from __future__ import annotations

import sys
from datetime import UTC, datetime

import httpx
import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    Flow,
    FlowCheckpoint,
    FlowCheckpointNotFoundError,
    FlowKind,
    FlowRunRecord,
    FlowStatus,
    InMemoryFlowRunStore,
    Stage,
)
from base_agent.events import InMemoryEventStore
from base_agent.execution import (
    ExecutionOperation,
    ExecutionRequestConflictError,
    ExecutionTargetKind,
    InMemoryExecutionStore,
    RecoveryDecision,
    RecoveryStrategy,
)
from base_agent.flows import SequentialFlow
from base_agent.models import RunStatus
from base_agent.run_store import AgentRunRecord, InMemoryRunStore
from base_agent.server import (
    AgentExecutionHandler,
    ExecutionWorker,
    FencedEventStore,
    FlowExecutionHandler,
    create_http_app,
)
from base_agent.testing import FakeModel


def _agent() -> Agent:
    return Agent(
        AgentDefinition(id="shared-worker-agent", version="1", instructions="work"),
        model=FakeModel.text("unused"),
    )


def _flow(*, event_store: InMemoryEventStore | None = None) -> SequentialFlow:
    agent = _agent()
    return Flow.sequence(
        id="shared-worker-flow",
        stages=(Stage(name="work", agent=agent),),
        event_store=event_store,
    )


def _flow_record(flow: SequentialFlow, flow_run_id: str = "flow-1") -> FlowRunRecord:
    return FlowRunRecord(
        flow_run_id=flow_run_id,
        flow_name="pipeline",
        flow_id=flow.id,
        flow_kind=flow.kind,
        definition_fingerprint=flow.definition_fingerprint,
        runtime_build_id="build-1",
    )


async def test_flow_checkpoint_is_fenced_by_the_shared_execution_attempt() -> None:
    runs = InMemoryRunStore()
    flows = InMemoryFlowRunStore()
    events = InMemoryEventStore()
    executions = InMemoryExecutionStore(
        runs,
        flow_store=flows,
        event_store=events,
    )
    flow = _flow()
    await executions.enqueue_flow(
        _flow_record(flow),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="flow-submit-1",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
        target_kinds=(ExecutionTargetKind.FLOW_RUN,),
    )
    assert claimed is not None
    child_events = FencedEventStore(events, executions, claimed.lease)
    child_terminal = await child_events.emit(
        "child-1",
        "run.succeeded",
        {"text": "child done"},
    )
    assert child_terminal.kind == "run.succeeded"
    checkpoint = FlowCheckpoint(
        flow_run_id="flow-1",
        flow_id=flow.id,
        flow_kind=FlowKind.SEQUENTIAL,
        definition_fingerprint=flow.definition_fingerprint,
        revision=1,
        cursor={"prompt": "work", "next_stage_index": 0},
        stage="work",
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
    )

    saved = await executions.save_flow_checkpoint(claimed.lease, checkpoint)
    waiting = await executions.settle(
        claimed.lease,
        status=FlowStatus.WAITING,
        pending_input={
            "request_id": "request-1",
            "kind": "tool_input",
            "tool_call_id": "call-1",
            "tool_name": "ask_user",
            "prompt": "Which region?",
            "metadata": {},
        },
        checkpoint_revision=saved.revision,
        waiting_child_run_id="child-1",
    )

    assert waiting.status is FlowStatus.WAITING
    assert waiting.checkpoint_revision == 1
    assert await executions.load_flow_checkpoint("flow-1") == checkpoint
    terminal = (await events.list("flow-1"))[-1]
    assert terminal.kind == "flow.waiting"
    assert terminal.payload["request_id"] == "request-1"
    assert terminal.payload["input_kind"] == "tool_input"
    assert terminal.payload["agent_run_id"] == "child-1"

    await executions.enqueue_flow_continuation(
        "flow-1",
        operation=ExecutionOperation.RESUME,
        payload={"input": "APAC"},
        idempotency_key="flow-resume-1",
    )
    continuation = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
        target_kinds=(ExecutionTargetKind.FLOW_RUN,),
    )
    assert continuation is not None
    recovery = await executions.interrupt(
        continuation.lease,
        reason_code="worker_shutdown",
    )
    restarted, _ = await executions.decide_recovery(
        "flow-1",
        target_kind=ExecutionTargetKind.FLOW_RUN,
        decision=RecoveryDecision.RETRY,
        strategy=RecoveryStrategy.RESTART,
        subject_id="operator-1",
        reason_code="verified_safe",
        expected_revision=recovery.revision,
        idempotency_key="flow-restart-1",
    )
    assert restarted.checkpoint_revision is None
    try:
        await executions.load_flow_checkpoint("flow-1")
    except FlowCheckpointNotFoundError:
        pass
    else:
        raise AssertionError("Flow restart retained a stale checkpoint")


async def test_one_execution_worker_dispatches_agent_and_flow_requests() -> None:
    runs = InMemoryRunStore()
    flows = InMemoryFlowRunStore()
    executions = InMemoryExecutionStore(runs, flow_store=flows)
    agent_record = AgentRunRecord(
        run_id="run-1",
        agent_id="shared-worker-agent",
        agent_version="1",
        snapshot_fingerprint="snapshot",
        runtime_build_id="build-1",
    )
    await executions.enqueue_run(
        agent_record,
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "agent work"},
        idempotency_key="agent-submit",
    )
    flow = _flow()
    await executions.enqueue_flow(
        _flow_record(flow),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "flow work"},
        idempotency_key="flow-submit",
    )
    success = (
        "import json,sys; json.load(sys.stdin); "
        "print(json.dumps({'status':'succeeded','output':'ok','text':'ok'}))"
    )
    command = (sys.executable, "-c", success)
    worker = ExecutionWorker(
        executions,
        handlers=(
            AgentExecutionHandler(command),
            FlowExecutionHandler(command),
        ),
        runtime_build_id="build-1",
        lease_seconds=1,
        heartbeat_interval_seconds=0.1,
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is True
    assert (await runs.get("run-1")).status is RunStatus.SUCCEEDED
    assert (await flows.get("flow-1")).status is FlowStatus.SUCCEEDED


async def test_agent_and_flow_share_the_idempotency_key_namespace() -> None:
    runs = InMemoryRunStore()
    flows = InMemoryFlowRunStore()
    executions = InMemoryExecutionStore(runs, flow_store=flows)
    await executions.enqueue_run(
        AgentRunRecord(
            run_id="shared-id",
            agent_id="shared-worker-agent",
            agent_version="1",
            snapshot_fingerprint="snapshot",
            runtime_build_id="build-1",
        ),
        operation=ExecutionOperation.EXECUTE,
        payload={"prompt": "work"},
        idempotency_key="shared-submission-key",
    )

    with pytest.raises(ExecutionRequestConflictError):
        await executions.enqueue_flow(
            _flow_record(_flow(), flow_run_id="shared-id"),
            operation=ExecutionOperation.EXECUTE,
            payload={"prompt": "work"},
            idempotency_key="shared-submission-key",
        )


async def test_queued_flow_http_submission_uses_durable_flow_control_state() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    flows = InMemoryFlowRunStore()
    executions = InMemoryExecutionStore(
        runs,
        flow_store=flows,
        event_store=events,
    )
    flow = _flow(event_store=events)
    app = create_http_app(
        flows={"pipeline": flow},
        event_store=events,
        flow_store=flows,
        execution_store=executions,
        execution_mode="queued",
        runtime_build_id="build-1",
        operator_authorizer=lambda request, subject_id: None,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/v1/flows/pipeline",
            headers={"Idempotency-Key": "flow-http-1"},
            json={"prompt": "work"},
        )
        repeated = await client.post(
            "/v1/flows/pipeline",
            headers={"Idempotency-Key": "flow-http-1"},
            json={"prompt": "work"},
        )
        current = await client.get(
            f"/v1/flows/{first.json()['flow_run_id']}"
        )
        claimed = await executions.claim_next(
            owner_id="worker-1",
            runtime_route="default",
            runtime_build_id="build-1",
            lease_seconds=30,
            target_kinds=(ExecutionTargetKind.FLOW_RUN,),
        )
        assert claimed is not None
        await executions.interrupt(
            claimed.lease,
            reason_code="worker_shutdown",
        )
        recovery = await client.get(
            f"/v1/flows/{first.json()['flow_run_id']}"
        )
        recovered = await client.post(
            f"/v1/operator/flows/{first.json()['flow_run_id']}/recovery-decisions",
            json={
                "decision": "retry",
                "strategy": "restart",
                "expected_revision": recovery.json()["recovery"]["revision"],
                "subject_id": "operator-1",
                "reason_code": "verified_safe",
                "idempotency_key": "flow-ticket-1",
            },
        )

    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert repeated.json() == first.json()
    assert current.status_code == 200
    assert current.json()["status"] == "queued"
    assert recovery.json()["status"] == "recovery_required"
    assert recovery.json()["recovery"]["reason_code"] == "worker_shutdown"
    assert recovered.status_code == 200
    assert recovered.json()["status"] == "queued"
