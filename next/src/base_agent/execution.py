"""Durable execution requests, attempts, fencing leases, and recovery decisions."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from base_agent.events import EventStore
from base_agent.models import RunStatus
from base_agent.run_store import (
    AgentRunRecord,
    RunStore,
    RunTransitionError,
)


class ExecutionTargetKind(StrEnum):
    AGENT_RUN = "agent_run"
    FLOW_RUN = "flow_run"


class ExecutionOperation(StrEnum):
    EXECUTE = "execute"
    RESUME = "resume"
    CONFIRM = "confirm"
    RECOVER = "recover"


class ExecutionRequestStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    DISCARDED = "discarded"


class ExecutionAttemptStatus(StrEnum):
    CLAIMED = "claimed"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    SUSPENDED = "suspended"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    LEASE_EXPIRED = "lease_expired"


class RecoveryDecision(StrEnum):
    RETRY = "retry"
    CANCEL = "cancel"
    FAIL = "fail"


class RecoveryStrategy(StrEnum):
    FROM_CHECKPOINT = "from_checkpoint"
    FROM_STAGE = "from_stage"
    RESTART = "restart"


class ExecutionStoreError(RuntimeError):
    """Base error for durable execution ownership operations."""


class ExecutionLeaseLostError(ExecutionStoreError):
    """The caller no longer owns the current live Attempt."""


class ExecutionRequestConflictError(ExecutionStoreError):
    """An idempotency key or target state conflicts with the requested intent."""


class ExecutionRequestNotFoundError(ExecutionStoreError, LookupError):
    """No matching ExecutionRequest exists."""


class ExecutionAttemptNotFoundError(ExecutionStoreError, LookupError):
    """No matching ExecutionAttempt exists."""


class ExecutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN
    target_id: str = Field(min_length=1)
    operation: ExecutionOperation
    status: ExecutionRequestStatus = ExecutionRequestStatus.PENDING
    payload: dict[str, Any] = Field(default_factory=dict)
    runtime_route: str = Field(default="default", min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    expected_target_revision: int = Field(ge=1)
    available_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    claimed_attempt_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("available_at", "created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ExecutionRequest timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> ExecutionRequest:
        if self.updated_at < self.created_at or self.available_at < self.created_at:
            raise ValueError("ExecutionRequest timestamps are out of order")
        return self


class ExecutionAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN
    target_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    attempt_number: int = Field(ge=1)
    status: ExecutionAttemptStatus = ExecutionAttemptStatus.CLAIMED
    owner_id: str = Field(min_length=1)
    fencing_token: str = Field(min_length=1)
    runtime_build_id: str = Field(min_length=1)
    child_pid: int | None = Field(default=None, ge=1)
    claimed_at: datetime
    started_at: datetime | None = None
    heartbeat_at: datetime
    lease_expires_at: datetime
    ended_at: datetime | None = None
    reason_code: str | None = None
    revision: int = Field(default=1, ge=1)

    @field_validator(
        "claimed_at",
        "started_at",
        "heartbeat_at",
        "lease_expires_at",
        "ended_at",
    )
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("ExecutionAttempt timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def lease_and_lifecycle_are_ordered(self) -> ExecutionAttempt:
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("ExecutionAttempt lease must expire after its heartbeat")
        if self.started_at is not None and self.started_at < self.claimed_at:
            raise ValueError("ExecutionAttempt cannot start before it is claimed")
        if self.ended_at is not None and self.ended_at < self.claimed_at:
            raise ValueError("ExecutionAttempt cannot end before it is claimed")
        return self


class ExecutionLease(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    request_id: str
    target_id: str
    owner_id: str
    fencing_token: str
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def expiration_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("ExecutionLease expiration must be timezone-aware")
        return value


class ClaimedExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: ExecutionRequest
    attempt: ExecutionAttempt
    lease: ExecutionLease
    run: AgentRunRecord


class RecoveryDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN
    target_id: str = Field(min_length=1)
    source_attempt_id: str | None = None
    decision: RecoveryDecision
    strategy: RecoveryStrategy | None = None
    subject_id: str = Field(min_length=1)
    reason_code: str = Field(min_length=1)
    expected_target_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    created_request_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RecoveryTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run: AgentRunRecord
    attempt: ExecutionAttempt


class AttemptHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease: ExecutionLease
    cancel_requested: bool


@runtime_checkable
class ExecutionStore(Protocol):
    """Atomic queue, Attempt lease, and recovery operations for Agent Runs."""

    async def enqueue_run(
        self,
        record: AgentRunRecord,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, ExecutionRequest]: ...

    async def enqueue_continuation(
        self,
        run_id: str,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, ExecutionRequest]: ...

    async def claim_next(
        self,
        *,
        owner_id: str,
        runtime_route: str,
        runtime_build_id: str,
        lease_seconds: float,
    ) -> ClaimedExecution | None: ...

    async def mark_started(
        self,
        lease: ExecutionLease,
        *,
        child_pid: int | None = None,
    ) -> ExecutionAttempt: ...

    async def assert_live(self, lease: ExecutionLease) -> None: ...

    async def renew(
        self,
        lease: ExecutionLease,
        *,
        lease_seconds: float,
    ) -> AttemptHeartbeat: ...

    async def settle(
        self,
        lease: ExecutionLease,
        *,
        status: RunStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: Mapping[str, Any] | None = None,
        checkpoint_revision: int | None = None,
    ) -> AgentRunRecord: ...

    async def interrupt(self, lease: ExecutionLease, *, reason_code: str) -> AgentRunRecord: ...

    async def block(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
        error: str,
    ) -> AgentRunRecord: ...

    async def reconcile_expired(self) -> tuple[RecoveryTransition, ...]: ...

    async def latest_attempt(self, run_id: str) -> ExecutionAttempt | None: ...

    async def request_cancel(self, run_id: str) -> AgentRunRecord: ...

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
    ) -> tuple[AgentRunRecord, RecoveryDecisionRecord]: ...


class MutableRunStore(RunStore, Protocol):
    """Internal CAS extension used by the in-memory ExecutionStore adapter."""

    async def transition(
        self,
        run_id: str,
        *,
        expected_statuses: Collection[RunStatus],
        status: RunStatus,
        expected_revision: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> AgentRunRecord: ...


class InMemoryExecutionStore:
    """Reference ExecutionStore used by tests and single-process compositions."""

    def __init__(
        self,
        run_store: MutableRunStore,
        *,
        clock: Callable[[], datetime] | None = None,
        event_store: EventStore | None = None,
    ) -> None:
        self.run_store = run_store
        self._clock = clock or (lambda: datetime.now(UTC))
        self._event_store = event_store
        self._requests: dict[str, ExecutionRequest] = {}
        self._attempts: dict[str, ExecutionAttempt] = {}
        self._decisions: dict[str, RecoveryDecisionRecord] = {}
        self._lock = asyncio.Lock()

    async def enqueue_run(
        self,
        record: AgentRunRecord,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, ExecutionRequest]:
        async with self._lock:
            existing = self._request_by_key(idempotency_key)
            if existing is not None:
                if (
                    existing.target_id != record.run_id
                    or existing.operation is not operation
                    or existing.payload != _json_mapping(payload)
                ):
                    raise ExecutionRequestConflictError("idempotency key has different intent")
                return await self.run_store.get(record.run_id), existing
            now = self._now()
            request = ExecutionRequest(
                target_id=record.run_id,
                operation=operation,
                payload=_json_mapping(payload),
                runtime_route=record.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=record.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = record.model_copy(
                update={
                    "status": RunStatus.QUEUED,
                    "revision": record.revision + 1,
                    "current_request_id": request.request_id,
                    "current_attempt_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            await self.run_store.create(queued)
            self._requests[request.request_id] = request
            return queued, request

    async def enqueue_continuation(
        self,
        run_id: str,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, ExecutionRequest]:
        if operation not in {ExecutionOperation.RESUME, ExecutionOperation.CONFIRM}:
            raise ValueError("continuation operation must be resume or confirm")
        async with self._lock:
            existing = self._request_by_key(idempotency_key)
            if existing is not None:
                if (
                    existing.target_id != run_id
                    or existing.operation is not operation
                    or existing.payload != _json_mapping(payload)
                ):
                    raise ExecutionRequestConflictError(
                        "idempotency key has different intent"
                    )
                return await self.run_store.get(run_id), existing
            record = await self.run_store.get(run_id)
            if record.status is not RunStatus.WAITING:
                raise RunTransitionError(
                    f"Run '{run_id}' cannot continue from status '{record.status.value}'"
                )
            now = self._now()
            request = ExecutionRequest(
                target_id=run_id,
                operation=operation,
                payload=_json_mapping(payload),
                runtime_route=record.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=record.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = await self.run_store.transition(
                run_id,
                expected_statuses={RunStatus.WAITING},
                expected_revision=record.revision,
                status=RunStatus.QUEUED,
                updates={"current_request_id": request.request_id},
            )
            self._requests[request.request_id] = request
            return queued, request

    async def claim_next(
        self,
        *,
        owner_id: str,
        runtime_route: str,
        runtime_build_id: str,
        lease_seconds: float,
    ) -> ClaimedExecution | None:
        _validate_lease(owner_id, lease_seconds)
        async with self._lock:
            now = self._now()
            request = next(
                (
                    item
                    for item in sorted(self._requests.values(), key=lambda value: value.created_at)
                    if item.status is ExecutionRequestStatus.PENDING
                    and item.runtime_route == runtime_route
                    and item.available_at <= now
                ),
                None,
            )
            if request is None:
                return None
            run = await self.run_store.get(request.target_id)
            if (
                run.status is not RunStatus.QUEUED
                or run.current_request_id != request.request_id
                or run.revision != request.expected_target_revision
            ):
                self._requests[request.request_id] = request.model_copy(
                    update={"status": ExecutionRequestStatus.DISCARDED, "updated_at": now}
                )
                return None
            number = 1 + sum(
                attempt.target_id == run.run_id for attempt in self._attempts.values()
            )
            token = str(uuid4())
            expires = now + timedelta(seconds=lease_seconds)
            attempt = ExecutionAttempt(
                target_id=run.run_id,
                request_id=request.request_id,
                attempt_number=number,
                owner_id=owner_id,
                fencing_token=token,
                runtime_build_id=runtime_build_id,
                claimed_at=now,
                heartbeat_at=now,
                lease_expires_at=expires,
            )
            claimed_request = request.model_copy(
                update={
                    "status": ExecutionRequestStatus.CLAIMED,
                    "claimed_attempt_id": attempt.attempt_id,
                    "updated_at": now,
                }
            )
            running = await self.run_store.transition(
                run.run_id,
                expected_statuses={RunStatus.QUEUED},
                expected_revision=run.revision,
                status=RunStatus.RUNNING,
                updates={
                    "current_attempt_id": attempt.attempt_id,
                    "recovery_reason_code": None,
                    "recovery_detected_at": None,
                    "error": None,
                },
            )
            self._requests[request.request_id] = claimed_request
            self._attempts[attempt.attempt_id] = attempt
            if self._event_store is not None:
                await self._event_store.emit(
                    run.run_id,
                    "execution.attempt.claimed",
                    {
                        "execution_attempt_id": attempt.attempt_id,
                        "request_id": request.request_id,
                        "attempt_number": attempt.attempt_number,
                        "owner_id": attempt.owner_id,
                        "runtime_build_id": attempt.runtime_build_id,
                    },
                )
            return ClaimedExecution(
                request=claimed_request,
                attempt=attempt,
                lease=ExecutionLease(
                    attempt_id=attempt.attempt_id,
                    request_id=request.request_id,
                    target_id=run.run_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    expires_at=expires,
                ),
                run=running,
            )

    async def mark_started(
        self,
        lease: ExecutionLease,
        *,
        child_pid: int | None = None,
    ) -> ExecutionAttempt:
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            started = attempt.model_copy(
                update={
                    "status": ExecutionAttemptStatus.RUNNING,
                    "child_pid": child_pid,
                    "started_at": now,
                    "revision": attempt.revision + 1,
                }
            )
            self._attempts[attempt.attempt_id] = started
            return started

    async def assert_live(self, lease: ExecutionLease) -> None:
        async with self._lock:
            attempt = self._owned_attempt(lease)
            if attempt.lease_expires_at <= self._now():
                raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' expired")

    async def renew(
        self,
        lease: ExecutionLease,
        *,
        lease_seconds: float,
    ) -> AttemptHeartbeat:
        _validate_lease(lease.owner_id, lease_seconds)
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' expired")
            expires = now + timedelta(seconds=lease_seconds)
            updated = attempt.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": expires,
                    "revision": attempt.revision + 1,
                }
            )
            self._attempts[attempt.attempt_id] = updated
            run = await self.run_store.get(lease.target_id)
            return AttemptHeartbeat(
                lease=lease.model_copy(update={"expires_at": expires}),
                cancel_requested=run.cancel_requested,
            )

    async def settle(
        self,
        lease: ExecutionLease,
        *,
        status: RunStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: Mapping[str, Any] | None = None,
        checkpoint_revision: int | None = None,
    ) -> AgentRunRecord:
        terminal_attempt = {
            RunStatus.WAITING: ExecutionAttemptStatus.SUSPENDED,
            RunStatus.SUCCEEDED: ExecutionAttemptStatus.SUCCEEDED,
            RunStatus.FAILED: ExecutionAttemptStatus.FAILED,
            RunStatus.CANCELLED: ExecutionAttemptStatus.CANCELLED,
        }.get(status)
        if terminal_attempt is None:
            raise RunTransitionError(f"Attempt cannot settle logical Run as '{status.value}'")
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' expired")
            run = await self.run_store.get(lease.target_id)
            if run.current_attempt_id != attempt.attempt_id:
                raise ExecutionLeaseLostError("Attempt is no longer current for the Run")
            updates = {
                "current_attempt_id": None,
                "checkpoint_revision": checkpoint_revision,
                "pending_input": _json_mapping(pending_input) if pending_input else None,
                "output": _json_value(output),
                "text": text,
                "error": error,
            }
            settled = await self.run_store.transition(
                run.run_id,
                expected_statuses={RunStatus.RUNNING},
                expected_revision=run.revision,
                status=status,
                updates=updates,
            )
            self._attempts[attempt.attempt_id] = attempt.model_copy(
                update={
                    "status": terminal_attempt,
                    "ended_at": now,
                    "revision": attempt.revision + 1,
                }
            )
            request = self._requests[attempt.request_id]
            self._requests[request.request_id] = request.model_copy(
                update={"status": ExecutionRequestStatus.COMPLETED, "updated_at": now}
            )
            if self._event_store is not None:
                await self._event_store.emit(
                    run.run_id,
                    _terminal_event_kind(status),
                    _terminal_event_payload(
                        attempt.attempt_id,
                        status=status,
                        output=output,
                        text=text,
                        error=error,
                        checkpoint_revision=checkpoint_revision,
                    ),
                )
            return settled

    async def interrupt(self, lease: ExecutionLease, *, reason_code: str) -> AgentRunRecord:
        async with self._lock:
            attempt = self._owned_attempt(lease)
            return await self._recover_attempt(
                attempt,
                attempt_status=ExecutionAttemptStatus.INTERRUPTED,
                reason_code=reason_code,
            )

    async def block(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
        error: str,
    ) -> AgentRunRecord:
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' expired")
            run = await self.run_store.get(lease.target_id)
            if run.status is not RunStatus.RUNNING or run.current_attempt_id != attempt.attempt_id:
                raise ExecutionLeaseLostError("Attempt is no longer current for the Run")
            ended = attempt.model_copy(
                update={
                    "status": ExecutionAttemptStatus.FAILED,
                    "ended_at": now,
                    "reason_code": reason_code,
                    "revision": attempt.revision + 1,
                }
            )
            request = self._requests[attempt.request_id]
            blocked_request = request.model_copy(
                update={"status": ExecutionRequestStatus.BLOCKED, "updated_at": now}
            )
            blocked = await self.run_store.transition(
                run.run_id,
                expected_statuses={RunStatus.RUNNING},
                expected_revision=run.revision,
                status=RunStatus.BLOCKED,
                updates={
                    "current_attempt_id": None,
                    "recovery_reason_code": reason_code,
                    "recovery_detected_at": now,
                    "error": error,
                },
            )
            self._attempts[attempt.attempt_id] = ended
            self._requests[request.request_id] = blocked_request
            if self._event_store is not None:
                await self._event_store.emit(
                    run.run_id,
                    "execution.request.blocked",
                    {
                        "execution_attempt_id": attempt.attempt_id,
                        "request_id": request.request_id,
                        "reason_code": reason_code,
                    },
                )
                await self._event_store.emit(
                    run.run_id,
                    "run.blocked",
                    {
                        "execution_attempt_id": attempt.attempt_id,
                        "reason_code": reason_code,
                        "error": error,
                    },
                )
            return blocked

    async def reconcile_expired(self) -> tuple[RecoveryTransition, ...]:
        async with self._lock:
            now = self._now()
            recovered: list[RecoveryTransition] = []
            for attempt in tuple(self._attempts.values()):
                if not _active_attempt(attempt) or attempt.lease_expires_at > now:
                    continue
                run = await self._recover_attempt(
                    attempt,
                    attempt_status=ExecutionAttemptStatus.LEASE_EXPIRED,
                    reason_code="owner_lease_expired",
                )
                recovered.append(
                    RecoveryTransition(run=run, attempt=self._attempts[attempt.attempt_id])
                )
            return tuple(recovered)

    async def latest_attempt(self, run_id: str) -> ExecutionAttempt | None:
        async with self._lock:
            candidates = [item for item in self._attempts.values() if item.target_id == run_id]
            if not candidates:
                return None
            return max(candidates, key=lambda item: item.attempt_number).model_copy(deep=True)

    async def request_cancel(self, run_id: str) -> AgentRunRecord:
        async with self._lock:
            record = await self.run_store.get(run_id)
            if record.status is RunStatus.QUEUED:
                if record.current_request_id is not None:
                    request = self._requests[record.current_request_id]
                    self._requests[request.request_id] = request.model_copy(
                        update={
                            "status": ExecutionRequestStatus.CANCELLED,
                            "updated_at": self._now(),
                        }
                    )
                cancelled = await self.run_store.transition(
                    run_id,
                    expected_statuses={RunStatus.QUEUED},
                    status=RunStatus.CANCELLED,
                    updates={"cancel_requested": True, "error": "run cancellation requested"},
                )
                await self._emit_cancelled(cancelled)
                return cancelled
            if record.status is RunStatus.RUNNING:
                return await self.run_store.request_cancel(run_id)
            if record.status in {RunStatus.WAITING, RunStatus.RECOVERY_REQUIRED, RunStatus.BLOCKED}:
                cancelled = await self.run_store.transition(
                    run_id,
                    expected_statuses={record.status},
                    status=RunStatus.CANCELLED,
                    updates={
                        "cancel_requested": True,
                        "checkpoint_revision": None,
                        "pending_input": None,
                        "error": "run cancellation requested",
                    },
                )
                await self._emit_cancelled(cancelled)
                return cancelled
            if record.status is RunStatus.CANCELLED:
                return record
            raise RunTransitionError(
                f"Run '{run_id}' already reached status '{record.status.value}'"
            )

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
        async with self._lock:
            existing = self._decisions.get(idempotency_key)
            if existing is not None:
                _validate_recovery_intent(
                    existing,
                    run_id=run_id,
                    decision=decision,
                    strategy=strategy,
                    subject_id=subject_id,
                    reason_code=reason_code,
                    expected_revision=expected_revision,
                )
                return await self.run_store.get(run_id), existing
            run = await self.run_store.get(run_id)
            if run.status not in {RunStatus.RECOVERY_REQUIRED, RunStatus.BLOCKED}:
                raise RunTransitionError(
                    f"Run '{run_id}' does not require an Operator recovery decision"
                )
            if run.revision != expected_revision:
                raise RunTransitionError(
                    f"Run '{run_id}' revision changed from {expected_revision} to {run.revision}"
                )
            if decision is RecoveryDecision.RETRY and strategy is None:
                raise ValueError("retry recovery decision requires a strategy")
            if decision is not RecoveryDecision.RETRY and strategy is not None:
                raise ValueError("only retry recovery decision accepts a strategy")
            now = self._now()
            created_request: ExecutionRequest | None = None
            if decision is RecoveryDecision.RETRY:
                if strategy is RecoveryStrategy.FROM_STAGE:
                    raise ValueError("Agent Run recovery does not support from_stage")
                if strategy is RecoveryStrategy.FROM_CHECKPOINT and run.checkpoint_revision is None:
                    raise RunTransitionError("Run has no checkpoint to recover from")
                source = (
                    self._original_execute_request(run_id)
                    if strategy is RecoveryStrategy.RESTART
                    else self._requests.get(run.current_request_id or "")
                )
                if source is None:
                    raise ExecutionRequestNotFoundError(
                        f"interrupted request for Run '{run_id}' was not found"
                    )
                payload = dict(source.payload)
                payload["recovery_strategy"] = cast(RecoveryStrategy, strategy).value
                payload["recovery_operation"] = source.operation.value
                created_request = ExecutionRequest(
                    target_id=run_id,
                    operation=ExecutionOperation.RECOVER,
                    payload=payload,
                    runtime_route=run.runtime_route,
                    idempotency_key=_derived_idempotency_key("recovery", idempotency_key),
                    expected_target_revision=run.revision + 1,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
                updated = await self.run_store.transition(
                    run_id,
                    expected_statuses={run.status},
                    expected_revision=run.revision,
                    status=RunStatus.QUEUED,
                    updates={
                        "current_request_id": created_request.request_id,
                        "current_attempt_id": None,
                        "cancel_requested": False,
                    },
                )
                self._requests[created_request.request_id] = created_request
            else:
                target_status = (
                    RunStatus.CANCELLED
                    if decision is RecoveryDecision.CANCEL
                    else RunStatus.FAILED
                )
                updated = await self.run_store.transition(
                    run_id,
                    expected_statuses={run.status},
                    expected_revision=run.revision,
                    status=target_status,
                    updates={
                        "current_attempt_id": None,
                        "cancel_requested": decision is RecoveryDecision.CANCEL,
                        "checkpoint_revision": None,
                        "pending_input": None,
                        "error": reason_code,
                    },
                )
            latest = max(
                (item for item in self._attempts.values() if item.target_id == run_id),
                key=lambda item: item.attempt_number,
                default=None,
            )
            record = RecoveryDecisionRecord(
                target_id=run_id,
                source_attempt_id=latest.attempt_id if latest is not None else None,
                decision=decision,
                strategy=strategy,
                subject_id=subject_id,
                reason_code=reason_code,
                expected_target_revision=expected_revision,
                idempotency_key=idempotency_key,
                created_request_id=(
                    created_request.request_id if created_request is not None else None
                ),
                created_at=now,
            )
            self._decisions[idempotency_key] = record
            if self._event_store is not None and decision is not RecoveryDecision.RETRY:
                await self._event_store.emit(
                    run_id,
                    _terminal_event_kind(updated.status),
                    _terminal_event_payload(
                        latest.attempt_id if latest is not None else None,
                        status=updated.status,
                        output=None,
                        text="",
                        error=updated.error,
                        checkpoint_revision=None,
                    ),
                )
            return updated, record

    async def _emit_cancelled(self, record: AgentRunRecord) -> None:
        if self._event_store is not None:
            await self._event_store.emit(
                record.run_id,
                "run.cancelled",
                {"error": record.error, "source": "execution_store"},
            )

    async def _recover_attempt(
        self,
        attempt: ExecutionAttempt,
        *,
        attempt_status: ExecutionAttemptStatus,
        reason_code: str,
    ) -> AgentRunRecord:
        now = self._now()
        ended = attempt.model_copy(
            update={
                "status": attempt_status,
                "ended_at": now,
                "reason_code": reason_code,
                "revision": attempt.revision + 1,
            }
        )
        self._attempts[attempt.attempt_id] = ended
        request = self._requests[attempt.request_id]
        self._requests[request.request_id] = request.model_copy(
            update={"status": ExecutionRequestStatus.INTERRUPTED, "updated_at": now}
        )
        run = await self.run_store.get(attempt.target_id)
        if run.current_attempt_id != attempt.attempt_id or run.status is not RunStatus.RUNNING:
            return run
        recovery = await self.run_store.transition(
            run.run_id,
            expected_statuses={RunStatus.RUNNING},
            expected_revision=run.revision,
            status=RunStatus.RECOVERY_REQUIRED,
            updates={
                "current_attempt_id": None,
                "recovery_reason_code": reason_code,
                "recovery_detected_at": now,
                "error": "Execution ownership was lost; Operator decision is required",
            },
        )
        if self._event_store is not None:
            await self._event_store.emit(
                run.run_id,
                (
                    "execution.attempt.lease_expired"
                    if attempt_status is ExecutionAttemptStatus.LEASE_EXPIRED
                    else "execution.attempt.interrupted"
                ),
                {
                    "execution_attempt_id": attempt.attempt_id,
                    "owner_id": attempt.owner_id,
                    "runtime_build_id": attempt.runtime_build_id,
                    "reason_code": reason_code,
                },
            )
            await self._event_store.emit(
                run.run_id,
                "run.recovery_required",
                {
                    "execution_attempt_id": attempt.attempt_id,
                    "reason_code": reason_code,
                    "revision": recovery.revision,
                },
            )
        return recovery

    def _owned_attempt(self, lease: ExecutionLease) -> ExecutionAttempt:
        try:
            attempt = self._attempts[lease.attempt_id]
        except KeyError as exc:
            raise ExecutionAttemptNotFoundError(lease.attempt_id) from exc
        if (
            not _active_attempt(attempt)
            or attempt.owner_id != lease.owner_id
            or attempt.fencing_token != lease.fencing_token
        ):
            raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' is not owned")
        return attempt

    def _request_by_key(self, key: str) -> ExecutionRequest | None:
        return next((item for item in self._requests.values() if item.idempotency_key == key), None)

    def _original_execute_request(self, run_id: str) -> ExecutionRequest:
        request = next(
            (
                item
                for item in self._requests.values()
                if item.target_id == run_id and item.operation is ExecutionOperation.EXECUTE
            ),
            None,
        )
        if request is None:
            raise ExecutionRequestNotFoundError(
                f"original execute request for Run '{run_id}' was not found"
            )
        return request

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("ExecutionStore clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _active_attempt(attempt: ExecutionAttempt) -> bool:
    return attempt.status in {
        ExecutionAttemptStatus.CLAIMED,
        ExecutionAttemptStatus.STARTING,
        ExecutionAttemptStatus.RUNNING,
        ExecutionAttemptStatus.STOPPING,
    }


def _validate_lease(owner_id: str, lease_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Execution lease owner_id must not be blank")
    if lease_seconds <= 0:
        raise ValueError("Execution lease_seconds must be greater than zero")


def _json_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _json_value(dict(value))
    return cast(dict[str, Any], result)


def _json_value(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Execution values must contain only finite JSON") from exc


def _terminal_event_kind(status: RunStatus) -> str:
    return {
        RunStatus.WAITING: "run.waiting",
        RunStatus.SUCCEEDED: "run.succeeded",
        RunStatus.FAILED: "run.failed",
        RunStatus.CANCELLED: "run.cancelled",
    }[status]


def _terminal_event_payload(
    attempt_id: str | None,
    *,
    status: RunStatus,
    output: Any | None,
    text: str | None,
    error: str | None,
    checkpoint_revision: int | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if attempt_id is not None:
        payload["execution_attempt_id"] = attempt_id
    if status is RunStatus.SUCCEEDED:
        payload.update(output=_json_value(output), text=text or "")
    elif status in {RunStatus.FAILED, RunStatus.CANCELLED}:
        payload.update(error=error, text=text or "")
    else:
        payload["checkpoint_revision"] = checkpoint_revision
    return payload


def _validate_recovery_intent(
    record: RecoveryDecisionRecord,
    *,
    run_id: str,
    decision: RecoveryDecision,
    strategy: RecoveryStrategy | None,
    subject_id: str,
    reason_code: str,
    expected_revision: int,
) -> None:
    if (
        record.target_id != run_id
        or record.decision is not decision
        or record.strategy is not strategy
        or record.subject_id != subject_id
        or record.reason_code != reason_code
        or record.expected_target_revision != expected_revision
    ):
        raise ExecutionRequestConflictError("idempotency key has different intent")


def _derived_idempotency_key(namespace: str, value: str) -> str:
    digest = hashlib.sha256(value.encode()).hexdigest()
    return f"{namespace}:{digest}"
