from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    Artifact,
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
    ArtifactReceiptStatus,
    ExecutionOperation,
    ExecutionRequestConflictError,
    ExecutionTargetKind,
    InMemoryExecutionStore,
    RecoveryDecision,
    RecoveryStrategy,
)
from base_agent.flows import SequentialFlow
from base_agent.flows.checkpoints import strict_json_value
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


async def test_verified_artifact_and_checkpointed_side_effect_gate_flow_recovery() -> None:
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
        idempotency_key="flow-artifact-submit",
    )
    claimed = await executions.claim_next(
        owner_id="worker-1",
        runtime_route="default",
        runtime_build_id="build-1",
        lease_seconds=30,
        target_kinds=(ExecutionTargetKind.FLOW_RUN,),
    )
    assert claimed is not None
    fenced = FencedEventStore(events, executions, claimed.lease)
    artifact = Artifact(
        id="artifact-1",
        name="dataset",
        media_type="text/csv",
        size_bytes=8,
        sha256="a" * 64,
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        run_id="child-query",
        agent_id="query-agent",
        flow_run_id="flow-1",
        invocation_id="flow-1:work:1",
        stage="work",
        attempt=1,
        tool_call_id="write-1",
        location="runs/flow-1/dataset.csv",
    )
    await fenced.emit(
        "flow-1",
        "artifact.created",
        {
            "artifact_id": artifact.id,
            "name": artifact.name,
            "media_type": artifact.media_type,
            "size_bytes": artifact.size_bytes,
            "sha256": artifact.sha256,
            "producer_run_id": artifact.run_id,
            "producer_agent_id": artifact.agent_id,
            "flow_run_id": artifact.flow_run_id,
            "invocation_id": artifact.invocation_id,
            "stage": artifact.stage,
            "attempt": artifact.attempt,
            "tool_call_id": artifact.tool_call_id,
            "location": artifact.location,
            "source_artifact_ids": [],
        },
    )
    side_effect = {
        "tool_name": "publish_dataset",
        "tool_call_id": "publish-1",
        "side_effect": "mutating",
        "operation_id": "op:publish",
        "idempotency_key": "op:publish",
        "intent_sha256": "b" * 64,
        "stage": "work",
        "attempt": 1,
    }
    await fenced.emit("flow-1", "tool.started", side_effect)
    await fenced.emit(
        "flow-1",
        "tool.completed",
        {**side_effect, "status": "success"},
    )
    checkpoint = FlowCheckpoint(
        flow_run_id="flow-1",
        flow_id=flow.id,
        flow_kind=FlowKind.SEQUENTIAL,
        definition_fingerprint=flow.definition_fingerprint,
        revision=1,
        cursor={
            "prompt": "work",
            "next_stage_index": 0,
            "accepted_artifacts": [strict_json_value(artifact)],
        },
        stage="work",
        artifact_ids=(artifact.id,),
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        updated_at=datetime(2026, 8, 21, tzinfo=UTC),
    )
    bad_checkpoint = checkpoint.model_copy(
        update={
            "cursor": {
                **checkpoint.cursor,
                "accepted_artifacts": [
                    strict_json_value(replace(artifact, sha256="c" * 64))
                ],
            }
        }
    )
    with pytest.raises(
        ExecutionRequestConflictError,
        match="changed immutable metadata",
    ):
        await executions.save_flow_checkpoint(claimed.lease, bad_checkpoint)
    await executions.save_flow_checkpoint(claimed.lease, checkpoint)
    recovery = await executions.interrupt(
        claimed.lease,
        reason_code="worker_shutdown",
    )
    evidence = await executions.recovery_evidence(
        "flow-1",
        target_kind=ExecutionTargetKind.FLOW_RUN,
    )

    assert evidence.artifact_receipts[0].status is ArtifactReceiptStatus.VERIFIED
    assert evidence.artifact_receipts[0].checkpoint_revision == 1
    assert evidence.side_effect_evidence[0].checkpoint_revision == 1
    assert evidence.blockers_for(RecoveryStrategy.FROM_STAGE) == ()
    assert [
        blocker.code for blocker in evidence.blockers_for(RecoveryStrategy.RESTART)
    ] == ["unsafe_side_effect_replay"]

    retried, _ = await executions.decide_recovery(
        "flow-1",
        target_kind=ExecutionTargetKind.FLOW_RUN,
        decision=RecoveryDecision.RETRY,
        strategy=RecoveryStrategy.FROM_STAGE,
        subject_id="operator-1",
        reason_code="artifacts_verified",
        expected_revision=recovery.revision,
        idempotency_key="flow-artifact-retry",
    )
    assert retried.status is FlowStatus.QUEUED


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
