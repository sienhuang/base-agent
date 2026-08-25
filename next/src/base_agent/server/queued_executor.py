"""API-side executor that persists commands without running Agent code locally."""

from __future__ import annotations

import hashlib
from typing import cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from base_agent.agent import Agent
from base_agent.execution import (
    ExecutionAttempt,
    ExecutionOperation,
    ExecutionStore,
    RecoveryDecision,
    RecoveryDecisionRecord,
    RecoveryEvidenceSnapshot,
    RecoveryStrategy,
)
from base_agent.governance import ToolConfirmation
from base_agent.run_handle import RunHandle
from base_agent.run_store import AgentRunRecord, RunRecordNotFoundError, RunStore


class QueuedRunExecutor:
    """Submit durable work for an independent Worker process."""

    def __init__(
        self,
        agent: Agent,
        run_store: RunStore,
        execution_store: ExecutionStore,
        *,
        runtime_route: str = "default",
        runtime_build_id: str | None = None,
    ) -> None:
        if not runtime_route.strip():
            raise ValueError("runtime_route must not be blank")
        self.agent = agent
        self.run_store = run_store
        self.execution_store = execution_store
        self.runtime_route = runtime_route
        self.runtime_build_id = runtime_build_id or agent.snapshot.fingerprint

    async def startup(self) -> tuple[AgentRunRecord, ...]:
        return ()

    async def close(self) -> None:
        return None

    async def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        idempotency_key: str | None = None,
    ) -> AgentRunRecord:
        snapshot = self.agent.snapshot
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        run_id = (
            str(uuid5(NAMESPACE_URL, f"base-agent:{snapshot.agent_id}:{idempotency_key}"))
            if idempotency_key is not None
            else str(uuid4())
        )
        record = AgentRunRecord(
            run_id=run_id,
            agent_id=snapshot.agent_id,
            agent_version=snapshot.agent_version,
            snapshot_fingerprint=snapshot.fingerprint,
            runtime_build_id=self.runtime_build_id,
            runtime_route=self.runtime_route,
        )
        queued, _ = await self.execution_store.enqueue_run(
            record,
            operation=ExecutionOperation.EXECUTE,
            payload={"prompt": prompt, "stream": stream},
            idempotency_key=(
                _submission_idempotency_key(snapshot.agent_id, idempotency_key)
                if idempotency_key is not None
                else f"run:{run_id}:execute"
            ),
        )
        return queued

    async def get(self, run_id: str) -> AgentRunRecord:
        record = await self.run_store.get(run_id)
        self._validate_owner(record)
        return record

    def local_handle(self, run_id: str) -> RunHandle | None:
        del run_id
        return None

    async def resume(self, run_id: str, user_input: str) -> AgentRunRecord:
        record = await self.get(run_id)
        request_id = _pending_request_id(record)
        queued, _ = await self.execution_store.enqueue_continuation(
            run_id,
            operation=ExecutionOperation.RESUME,
            payload={"input": user_input},
            idempotency_key=f"run:{run_id}:resume:{request_id}",
        )
        return queued

    async def confirm(
        self,
        run_id: str,
        confirmation: ToolConfirmation,
    ) -> AgentRunRecord:
        record = await self.get(run_id)
        if confirmation.request_id != _pending_request_id(record):
            raise ValueError("confirmation request_id does not match the WAITING Run")
        queued, _ = await self.execution_store.enqueue_continuation(
            run_id,
            operation=ExecutionOperation.CONFIRM,
            payload={
                "request_id": confirmation.request_id,
                "decision": confirmation.decision.value,
                "subject_id": confirmation.subject_id,
                "reason_code": confirmation.reason_code,
            },
            idempotency_key=f"run:{run_id}:confirm:{confirmation.request_id}",
        )
        return queued

    async def cancel(self, run_id: str) -> AgentRunRecord:
        self._validate_owner(await self.run_store.get(run_id))
        return cast(
            AgentRunRecord,
            await self.execution_store.request_cancel(run_id),
        )

    async def latest_attempt(self, run_id: str) -> ExecutionAttempt | None:
        self._validate_owner(await self.run_store.get(run_id))
        return await self.execution_store.latest_attempt(run_id)

    async def recovery_evidence(self, run_id: str) -> RecoveryEvidenceSnapshot:
        self._validate_owner(await self.run_store.get(run_id))
        return await self.execution_store.recovery_evidence(run_id)

    async def decide_recovery(
        self,
        run_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, RecoveryDecisionRecord]:
        self._validate_owner(await self.run_store.get(run_id))
        return cast(
            tuple[AgentRunRecord, RecoveryDecisionRecord],
            await self.execution_store.decide_recovery(
                run_id,
                decision=decision,
                strategy=strategy,
                subject_id=subject_id,
                reason_code=reason_code,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )

    def _validate_owner(self, record: AgentRunRecord) -> None:
        if record.agent_id != self.agent.definition.id:
            raise RunRecordNotFoundError(
                f"Run record '{record.run_id}' does not belong to Agent "
                f"'{self.agent.definition.id}'"
            )


def _pending_request_id(record: AgentRunRecord) -> str:
    pending = record.pending_input or {}
    request_id = pending.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError(f"WAITING Run '{record.run_id}' has no pending request ID")
    return request_id


def _submission_idempotency_key(agent_id: str, key: str) -> str:
    digest = hashlib.sha256(f"{agent_id}:{key}".encode()).hexdigest()
    return f"run-submit:{digest}"
