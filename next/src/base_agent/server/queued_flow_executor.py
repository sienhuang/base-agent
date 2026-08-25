"""API-side Flow executor that persists work for the shared ExecutionWorker."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Protocol, cast, runtime_checkable
from uuid import NAMESPACE_URL, uuid4, uuid5

from base_agent.execution import (
    ExecutionAttempt,
    ExecutionOperation,
    ExecutionStore,
    ExecutionTargetKind,
    RecoveryDecision,
    RecoveryDecisionRecord,
    RecoveryEvidenceSnapshot,
    RecoveryStrategy,
)
from base_agent.flow_store import (
    FlowKind,
    FlowRunRecord,
    FlowRunRecordNotFoundError,
    FlowRunStore,
)
from base_agent.governance import ToolConfirmation


@runtime_checkable
class DurableFlowDefinition(Protocol):
    id: str
    kind: FlowKind
    definition_fingerprint: str


class QueuedFlowExecutor:
    """Submit named Flow Runs without executing orchestration in the API process."""

    def __init__(
        self,
        flows: Mapping[str, DurableFlowDefinition],
        flow_store: FlowRunStore,
        execution_store: ExecutionStore,
        *,
        runtime_route: str = "default",
        runtime_build_id: str,
    ) -> None:
        if not runtime_route.strip() or not runtime_build_id.strip():
            raise ValueError("runtime_route and runtime_build_id must not be blank")
        self.flows = dict(flows)
        self.flow_store = flow_store
        self.execution_store = execution_store
        self.runtime_route = runtime_route
        self.runtime_build_id = runtime_build_id

    async def start(
        self,
        name: str,
        prompt: str,
        *,
        stream: bool = False,
        idempotency_key: str | None = None,
    ) -> FlowRunRecord:
        flow = self._flow(name)
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        flow_run_id = (
            str(uuid5(NAMESPACE_URL, f"base-agent:flow:{flow.id}:{idempotency_key}"))
            if idempotency_key is not None
            else str(uuid4())
        )
        record = FlowRunRecord(
            flow_run_id=flow_run_id,
            flow_name=name,
            flow_id=flow.id,
            flow_kind=flow.kind,
            definition_fingerprint=flow.definition_fingerprint,
            runtime_build_id=self.runtime_build_id,
            runtime_route=self.runtime_route,
        )
        queued, _ = await self.execution_store.enqueue_flow(
            record,
            operation=ExecutionOperation.EXECUTE,
            payload={"prompt": prompt, "stream": stream},
            idempotency_key=(
                _submission_idempotency_key(flow.id, idempotency_key)
                if idempotency_key is not None
                else f"flow:{flow_run_id}:execute"
            ),
        )
        return queued

    async def get(self, flow_run_id: str) -> FlowRunRecord:
        record = await self.flow_store.get(flow_run_id)
        self._flow(record.flow_name, expected=record)
        return record

    async def resume(self, flow_run_id: str, user_input: str) -> FlowRunRecord:
        record = await self.get(flow_run_id)
        request_id = _pending_request_id(record)
        queued, _ = await self.execution_store.enqueue_flow_continuation(
            flow_run_id,
            operation=ExecutionOperation.RESUME,
            payload={"input": user_input},
            idempotency_key=f"flow:{flow_run_id}:resume:{request_id}",
        )
        return queued

    async def confirm(
        self,
        flow_run_id: str,
        confirmation: ToolConfirmation,
    ) -> FlowRunRecord:
        record = await self.get(flow_run_id)
        if confirmation.request_id != _pending_request_id(record):
            raise ValueError("confirmation request_id does not match the WAITING Flow")
        queued, _ = await self.execution_store.enqueue_flow_continuation(
            flow_run_id,
            operation=ExecutionOperation.CONFIRM,
            payload={
                "request_id": confirmation.request_id,
                "decision": confirmation.decision.value,
                "subject_id": confirmation.subject_id,
                "reason_code": confirmation.reason_code,
            },
            idempotency_key=f"flow:{flow_run_id}:confirm:{confirmation.request_id}",
        )
        return queued

    async def cancel(self, flow_run_id: str) -> FlowRunRecord:
        await self.get(flow_run_id)
        return cast(
            FlowRunRecord,
            await self.execution_store.request_cancel(
                flow_run_id,
                target_kind=ExecutionTargetKind.FLOW_RUN,
            ),
        )

    async def latest_attempt(self, flow_run_id: str) -> ExecutionAttempt | None:
        await self.get(flow_run_id)
        return await self.execution_store.latest_attempt(
            flow_run_id,
            target_kind=ExecutionTargetKind.FLOW_RUN,
        )

    async def recovery_evidence(self, flow_run_id: str) -> RecoveryEvidenceSnapshot:
        await self.get(flow_run_id)
        return await self.execution_store.recovery_evidence(
            flow_run_id,
            target_kind=ExecutionTargetKind.FLOW_RUN,
        )

    async def decide_recovery(
        self,
        flow_run_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, RecoveryDecisionRecord]:
        await self.get(flow_run_id)
        return cast(
            tuple[FlowRunRecord, RecoveryDecisionRecord],
            await self.execution_store.decide_recovery(
                flow_run_id,
                target_kind=ExecutionTargetKind.FLOW_RUN,
                decision=decision,
                strategy=strategy,
                subject_id=subject_id,
                reason_code=reason_code,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    def _flow(
        self,
        name: str,
        *,
        expected: FlowRunRecord | None = None,
    ) -> DurableFlowDefinition:
        try:
            flow = self.flows[name]
        except KeyError as exc:
            raise FlowRunRecordNotFoundError(
                f"Flow '{name}' is not registered by this Runtime"
            ) from exc
        if expected is not None and (
            expected.flow_id != flow.id
            or expected.flow_kind is not flow.kind
            or expected.definition_fingerprint != flow.definition_fingerprint
        ):
            raise FlowRunRecordNotFoundError(
                f"Flow Run '{expected.flow_run_id}' does not belong to the registered "
                f"Flow '{name}' definition"
            )
        return flow


def _pending_request_id(record: FlowRunRecord) -> str:
    pending = record.pending_input or {}
    request_id = pending.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError(f"WAITING Flow Run '{record.flow_run_id}' has no request ID")
    return request_id


def _submission_idempotency_key(flow_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{flow_id}:{key}".encode()).hexdigest()
    return f"flow-submit:{digest}"


__all__ = ["DurableFlowDefinition", "QueuedFlowExecutor"]
