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
from base_agent.flow_store import (
    FlowCheckpoint,
    FlowCheckpointStore,
    FlowRunRecord,
    FlowRunStore,
    FlowTransitionError,
)
from base_agent.flows.models import FlowStatus
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
    target: AgentRunRecord | FlowRunRecord

    @property
    def run(self) -> AgentRunRecord:
        """Backward-compatible Agent-only view used by older Worker adapters."""
        if not isinstance(self.target, AgentRunRecord):
            raise TypeError("claimed target is a Flow Run")
        return self.target


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

    target: AgentRunRecord | FlowRunRecord
    attempt: ExecutionAttempt

    @property
    def run(self) -> AgentRunRecord:
        if not isinstance(self.target, AgentRunRecord):
            raise TypeError("recovery target is a Flow Run")
        return self.target


class AttemptHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lease: ExecutionLease
    cancel_requested: bool


@runtime_checkable
class ExecutionStore(Protocol):
    """Atomic queue, Attempt lease, and recovery operations for Runs and Flows."""

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

    async def enqueue_flow(
        self,
        record: FlowRunRecord,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, ExecutionRequest]: ...

    async def enqueue_flow_continuation(
        self,
        flow_run_id: str,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, ExecutionRequest]: ...

    async def claim_next(
        self,
        *,
        owner_id: str,
        runtime_route: str,
        runtime_build_id: str,
        lease_seconds: float,
        target_kinds: Collection[ExecutionTargetKind] = (
            ExecutionTargetKind.AGENT_RUN,
        ),
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
        status: RunStatus | FlowStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: Mapping[str, Any] | None = None,
        checkpoint_revision: int | None = None,
        waiting_child_run_id: str | None = None,
    ) -> AgentRunRecord | FlowRunRecord: ...

    async def interrupt(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
    ) -> AgentRunRecord | FlowRunRecord: ...

    async def block(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
        error: str,
    ) -> AgentRunRecord | FlowRunRecord: ...

    async def reconcile_expired(self) -> tuple[RecoveryTransition, ...]: ...

    async def latest_attempt(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> ExecutionAttempt | None: ...

    async def request_cancel(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> AgentRunRecord | FlowRunRecord: ...

    async def decide_recovery(
        self,
        target_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> tuple[AgentRunRecord | FlowRunRecord, RecoveryDecisionRecord]: ...

    async def save_flow_checkpoint(
        self,
        lease: ExecutionLease,
        checkpoint: FlowCheckpoint,
    ) -> FlowCheckpoint: ...

    async def load_flow_checkpoint(self, flow_run_id: str) -> FlowCheckpoint: ...


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


class MutableFlowRunStore(FlowRunStore, FlowCheckpointStore, Protocol):
    """Internal Flow state and checkpoint boundary used by execution adapters."""


class InMemoryExecutionStore:
    """Reference ExecutionStore used by tests and single-process compositions."""

    def __init__(
        self,
        run_store: MutableRunStore,
        *,
        flow_store: MutableFlowRunStore | None = None,
        clock: Callable[[], datetime] | None = None,
        event_store: EventStore | None = None,
    ) -> None:
        self.run_store = run_store
        self.flow_store = flow_store
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
        if operation is not ExecutionOperation.EXECUTE:
            raise ValueError("a new Run requires the execute operation")
        async with self._lock:
            existing = self._request_by_key(idempotency_key)
            if existing is not None:
                if (
                    existing.target_kind is not ExecutionTargetKind.AGENT_RUN
                    or existing.target_id != record.run_id
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

    async def enqueue_flow(
        self,
        record: FlowRunRecord,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, ExecutionRequest]:
        if operation is not ExecutionOperation.EXECUTE:
            raise ValueError("a new Flow Run requires the execute operation")
        flow_store = self._require_flow_store()
        async with self._lock:
            existing = self._request_by_key(idempotency_key)
            if existing is not None:
                if (
                    existing.target_kind is not ExecutionTargetKind.FLOW_RUN
                    or existing.target_id != record.flow_run_id
                    or existing.operation is not operation
                    or existing.payload != _json_mapping(payload)
                ):
                    raise ExecutionRequestConflictError(
                        "idempotency key has different intent"
                    )
                return await flow_store.get(record.flow_run_id), existing
            now = self._now()
            request = ExecutionRequest(
                target_kind=ExecutionTargetKind.FLOW_RUN,
                target_id=record.flow_run_id,
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
                    "status": FlowStatus.QUEUED,
                    "revision": record.revision + 1,
                    "current_request_id": request.request_id,
                    "current_attempt_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            await flow_store.create(queued)
            self._requests[request.request_id] = request
            return queued, request

    async def enqueue_flow_continuation(
        self,
        flow_run_id: str,
        *,
        operation: ExecutionOperation,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, ExecutionRequest]:
        if operation not in {ExecutionOperation.RESUME, ExecutionOperation.CONFIRM}:
            raise ValueError("Flow continuation operation must be resume or confirm")
        flow_store = self._require_flow_store()
        async with self._lock:
            existing = self._request_by_key(idempotency_key)
            if existing is not None:
                if (
                    existing.target_kind is not ExecutionTargetKind.FLOW_RUN
                    or existing.target_id != flow_run_id
                    or existing.operation is not operation
                    or existing.payload != _json_mapping(payload)
                ):
                    raise ExecutionRequestConflictError(
                        "idempotency key has different intent"
                    )
                return await flow_store.get(flow_run_id), existing
            record = await flow_store.get(flow_run_id)
            if record.status is not FlowStatus.WAITING:
                raise FlowTransitionError(
                    f"Flow Run '{flow_run_id}' cannot continue from "
                    f"status '{record.status.value}'"
                )
            now = self._now()
            request = ExecutionRequest(
                target_kind=ExecutionTargetKind.FLOW_RUN,
                target_id=flow_run_id,
                operation=operation,
                payload=_json_mapping(payload),
                runtime_route=record.runtime_route,
                idempotency_key=idempotency_key,
                expected_target_revision=record.revision + 1,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
            queued = await flow_store.transition(
                flow_run_id,
                expected_statuses={FlowStatus.WAITING},
                expected_revision=record.revision,
                status=FlowStatus.QUEUED,
                updates={"current_request_id": request.request_id},
            )
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
                    existing.target_kind is not ExecutionTargetKind.AGENT_RUN
                    or existing.target_id != run_id
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
        target_kinds: Collection[ExecutionTargetKind] = (
            ExecutionTargetKind.AGENT_RUN,
        ),
    ) -> ClaimedExecution | None:
        _validate_lease(owner_id, lease_seconds)
        supported = frozenset(target_kinds)
        if not supported:
            raise ValueError("Execution Worker must support at least one target kind")
        async with self._lock:
            now = self._now()
            request = next(
                (
                    item
                    for item in sorted(self._requests.values(), key=lambda value: value.created_at)
                    if item.status is ExecutionRequestStatus.PENDING
                    and item.runtime_route == runtime_route
                    and item.target_kind in supported
                    and item.available_at <= now
                ),
                None,
            )
            if request is None:
                return None
            target = await self._target(request.target_kind, request.target_id)
            if (
                target.status.value != "queued"
                or target.current_request_id != request.request_id
                or target.revision != request.expected_target_revision
            ):
                self._requests[request.request_id] = request.model_copy(
                    update={"status": ExecutionRequestStatus.DISCARDED, "updated_at": now}
                )
                return None
            number = 1 + sum(
                attempt.target_kind is request.target_kind
                and attempt.target_id == request.target_id
                for attempt in self._attempts.values()
            )
            token = str(uuid4())
            expires = now + timedelta(seconds=lease_seconds)
            attempt = ExecutionAttempt(
                target_kind=request.target_kind,
                target_id=request.target_id,
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
            running = await self._transition_target(
                target,
                status="running",
                updates={
                    "current_attempt_id": attempt.attempt_id,
                    "recovery_reason_code": None,
                    "recovery_detected_at": None,
                    "error": None,
                    "pending_input": None,
                    "waiting_child_run_id": None,
                },
            )
            self._requests[request.request_id] = claimed_request
            self._attempts[attempt.attempt_id] = attempt
            if self._event_store is not None:
                await self._event_store.emit(
                    request.target_id,
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
                    target_id=request.target_id,
                    owner_id=owner_id,
                    fencing_token=token,
                    expires_at=expires,
                ),
                target=running,
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
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(
                    f"Attempt lease '{lease.attempt_id}' expired"
                )
            target = await self._target(attempt.target_kind, attempt.target_id)
            if (
                target.status.value != "running"
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for its target"
                )
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
            target = await self._target(attempt.target_kind, attempt.target_id)
            if (
                target.status.value != "running"
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for its target"
                )

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
            target = await self._target(attempt.target_kind, lease.target_id)
            if (
                target.status.value != "running"
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for its target"
                )
            expires = now + timedelta(seconds=lease_seconds)
            updated = attempt.model_copy(
                update={
                    "heartbeat_at": now,
                    "lease_expires_at": expires,
                    "revision": attempt.revision + 1,
                }
            )
            self._attempts[attempt.attempt_id] = updated
            return AttemptHeartbeat(
                lease=lease.model_copy(update={"expires_at": expires}),
                cancel_requested=target.cancel_requested,
            )

    async def settle(
        self,
        lease: ExecutionLease,
        *,
        status: RunStatus | FlowStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: Mapping[str, Any] | None = None,
        checkpoint_revision: int | None = None,
        waiting_child_run_id: str | None = None,
    ) -> AgentRunRecord | FlowRunRecord:
        terminal_attempt = {
            "waiting": ExecutionAttemptStatus.SUSPENDED,
            "succeeded": ExecutionAttemptStatus.SUCCEEDED,
            "failed": ExecutionAttemptStatus.FAILED,
            "cancelled": ExecutionAttemptStatus.CANCELLED,
        }.get(status.value)
        if terminal_attempt is None:
            raise RunTransitionError(
                f"Attempt cannot settle logical target as '{status.value}'"
            )
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' expired")
            target = await self._target(attempt.target_kind, lease.target_id)
            if target.current_attempt_id != attempt.attempt_id:
                raise ExecutionLeaseLostError("Attempt is no longer current for its target")
            updates = {
                "current_attempt_id": None,
                "checkpoint_revision": checkpoint_revision,
                "pending_input": _json_mapping(pending_input) if pending_input else None,
                "output": (
                    _json_value(output)
                    if attempt.target_kind is ExecutionTargetKind.AGENT_RUN
                    else (text if text is not None else _flow_output(output))
                ),
                "error": error,
            }
            if attempt.target_kind is ExecutionTargetKind.AGENT_RUN:
                updates["text"] = text
            else:
                updates["waiting_child_run_id"] = waiting_child_run_id
                if status.value != "waiting":
                    updates["checkpoint_stage"] = None
            settled = await self._transition_target(
                target,
                status=status.value,
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
                    request.target_id,
                    _target_terminal_event_kind(request.target_kind, status.value),
                    _target_terminal_event_payload(
                        attempt.attempt_id,
                        target_kind=request.target_kind,
                        status=status.value,
                        output=output,
                        text=text,
                        error=error,
                        checkpoint_revision=checkpoint_revision,
                        pending_input=pending_input,
                        waiting_child_run_id=waiting_child_run_id,
                    ),
                )
            if (
                request.target_kind is ExecutionTargetKind.FLOW_RUN
                and status.value in {"succeeded", "failed", "cancelled"}
            ):
                await self._require_flow_store().delete(request.target_id)
            return settled

    async def interrupt(
        self,
        lease: ExecutionLease,
        *,
        reason_code: str,
    ) -> AgentRunRecord | FlowRunRecord:
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
    ) -> AgentRunRecord | FlowRunRecord:
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' expired")
            target = await self._target(attempt.target_kind, lease.target_id)
            if target.status.value != "running" or target.current_attempt_id != attempt.attempt_id:
                raise ExecutionLeaseLostError("Attempt is no longer current for its target")
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
            blocked = await self._transition_target(
                target,
                status="blocked",
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
                    request.target_id,
                    "execution.request.blocked",
                    {
                        "execution_attempt_id": attempt.attempt_id,
                        "request_id": request.request_id,
                        "reason_code": reason_code,
                    },
                )
                await self._event_store.emit(
                    request.target_id,
                    (
                        "run.blocked"
                        if request.target_kind is ExecutionTargetKind.AGENT_RUN
                        else "flow.blocked"
                    ),
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
                target = await self._recover_attempt(
                    attempt,
                    attempt_status=ExecutionAttemptStatus.LEASE_EXPIRED,
                    reason_code="owner_lease_expired",
                )
                recovered.append(
                    RecoveryTransition(
                        target=target,
                        attempt=self._attempts[attempt.attempt_id],
                    )
                )
            return tuple(recovered)

    async def latest_attempt(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> ExecutionAttempt | None:
        async with self._lock:
            candidates = [
                item
                for item in self._attempts.values()
                if item.target_kind is target_kind and item.target_id == target_id
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda item: item.attempt_number).model_copy(deep=True)

    async def request_cancel(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> AgentRunRecord | FlowRunRecord:
        async with self._lock:
            record = await self._target(target_kind, target_id)
            if record.status.value == "queued":
                if record.current_request_id is not None:
                    request = self._requests[record.current_request_id]
                    self._requests[request.request_id] = request.model_copy(
                        update={
                            "status": ExecutionRequestStatus.CANCELLED,
                            "updated_at": self._now(),
                        }
                    )
                cancelled = await self._transition_target(
                    record,
                    status="cancelled",
                    updates={
                        "cancel_requested": True,
                        "checkpoint_revision": None,
                        "checkpoint_stage": None,
                        "waiting_child_run_id": None,
                        "pending_input": None,
                        "error": "execution cancellation requested",
                    },
                )
                await self._emit_cancelled(cancelled)
                return cancelled
            if record.status.value == "running":
                return await self._transition_target(
                    record,
                    status="running",
                    updates={"cancel_requested": True},
                )
            if record.status.value in {"waiting", "recovery_required", "blocked"}:
                cancelled = await self._transition_target(
                    record,
                    status="cancelled",
                    updates={
                        "cancel_requested": True,
                        "checkpoint_revision": None,
                        "checkpoint_stage": None,
                        "waiting_child_run_id": None,
                        "pending_input": None,
                        "error": "execution cancellation requested",
                    },
                )
                await self._emit_cancelled(cancelled)
                if target_kind is ExecutionTargetKind.FLOW_RUN:
                    await self._require_flow_store().delete(target_id)
                return cancelled
            if record.status.value == "cancelled":
                return record
            raise FlowTransitionError(
                f"execution target '{target_id}' already reached "
                f"status '{record.status.value}'"
            )

    async def decide_recovery(
        self,
        target_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> tuple[AgentRunRecord | FlowRunRecord, RecoveryDecisionRecord]:
        async with self._lock:
            existing = self._decisions.get(idempotency_key)
            if existing is not None:
                _validate_recovery_intent(
                    existing,
                    run_id=target_id,
                    decision=decision,
                    strategy=strategy,
                    subject_id=subject_id,
                    reason_code=reason_code,
                    expected_revision=expected_revision,
                )
                if existing.target_kind is not target_kind:
                    raise ExecutionRequestConflictError(
                        "recovery idempotency key targets another execution kind"
                    )
                return await self._target(target_kind, target_id), existing
            target = await self._target(target_kind, target_id)
            if target.status.value not in {"recovery_required", "blocked"}:
                raise FlowTransitionError(
                    f"execution target '{target_id}' does not require an "
                    "Operator recovery decision"
                )
            if target.revision != expected_revision:
                raise FlowTransitionError(
                    f"execution target '{target_id}' revision changed from "
                    f"{expected_revision} to {target.revision}"
                )
            if decision is RecoveryDecision.RETRY and strategy is None:
                raise ValueError("retry recovery decision requires a strategy")
            if decision is not RecoveryDecision.RETRY and strategy is not None:
                raise ValueError("only retry recovery decision accepts a strategy")
            now = self._now()
            created_request: ExecutionRequest | None = None
            if decision is RecoveryDecision.RETRY:
                if (
                    target_kind is ExecutionTargetKind.AGENT_RUN
                    and strategy is RecoveryStrategy.FROM_STAGE
                ):
                    raise ValueError("Agent Run recovery does not support from_stage")
                if (
                    strategy in {
                        RecoveryStrategy.FROM_CHECKPOINT,
                        RecoveryStrategy.FROM_STAGE,
                    }
                    and target.checkpoint_revision is None
                ):
                    raise FlowTransitionError(
                        "execution target has no checkpoint to recover from"
                    )
                if (
                    strategy is RecoveryStrategy.FROM_STAGE
                    and isinstance(target, FlowRunRecord)
                    and target.checkpoint_stage is None
                ):
                    raise FlowTransitionError("Flow Run has no durable Stage checkpoint")
                source = (
                    self._original_execute_request(target_id, target_kind=target_kind)
                    if strategy is RecoveryStrategy.RESTART
                    else self._requests.get(target.current_request_id or "")
                )
                if source is None:
                    raise ExecutionRequestNotFoundError(
                        f"interrupted request for target '{target_id}' was not found"
                    )
                payload = dict(source.payload)
                payload["recovery_strategy"] = cast(RecoveryStrategy, strategy).value
                payload["recovery_operation"] = source.operation.value
                created_request = ExecutionRequest(
                    target_kind=target_kind,
                    target_id=target_id,
                    operation=ExecutionOperation.RECOVER,
                    payload=payload,
                    runtime_route=target.runtime_route,
                    idempotency_key=_derived_idempotency_key("recovery", idempotency_key),
                    expected_target_revision=target.revision + 1,
                    available_at=now,
                    created_at=now,
                    updated_at=now,
                )
                updated = await self._transition_target(
                    target,
                    status="queued",
                    updates={
                        "current_request_id": created_request.request_id,
                        "current_attempt_id": None,
                        "cancel_requested": False,
                        **(
                            {
                                "checkpoint_revision": None,
                                "pending_input": None,
                            }
                            if strategy is RecoveryStrategy.RESTART
                            else {}
                        ),
                        **(
                            {
                                "checkpoint_stage": None,
                                "plan_revision": None,
                                "waiting_child_run_id": None,
                            }
                            if target_kind is ExecutionTargetKind.FLOW_RUN
                            and strategy is RecoveryStrategy.RESTART
                            else {}
                        ),
                    },
                )
                if (
                    target_kind is ExecutionTargetKind.FLOW_RUN
                    and strategy is RecoveryStrategy.RESTART
                ):
                    await self._require_flow_store().delete(target_id)
                self._requests[created_request.request_id] = created_request
            else:
                target_status = (
                    "cancelled"
                    if decision is RecoveryDecision.CANCEL
                    else "failed"
                )
                updated = await self._transition_target(
                    target,
                    status=target_status,
                    updates={
                        "current_attempt_id": None,
                        "cancel_requested": decision is RecoveryDecision.CANCEL,
                        "checkpoint_revision": None,
                        "checkpoint_stage": None,
                        "waiting_child_run_id": None,
                        "pending_input": None,
                        "error": reason_code,
                    },
                )
            latest = max(
                (
                    item
                    for item in self._attempts.values()
                    if item.target_kind is target_kind
                    and item.target_id == target_id
                ),
                key=lambda item: item.attempt_number,
                default=None,
            )
            record = RecoveryDecisionRecord(
                target_kind=target_kind,
                target_id=target_id,
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
                    target_id,
                    _target_terminal_event_kind(target_kind, updated.status.value),
                    _target_terminal_event_payload(
                        latest.attempt_id if latest is not None else None,
                        target_kind=target_kind,
                        status=updated.status.value,
                        output=None,
                        text="",
                        error=updated.error,
                        checkpoint_revision=None,
                    ),
                )
            if (
                target_kind is ExecutionTargetKind.FLOW_RUN
                and decision is not RecoveryDecision.RETRY
            ):
                await self._require_flow_store().delete(target_id)
            return updated, record

    async def save_flow_checkpoint(
        self,
        lease: ExecutionLease,
        checkpoint: FlowCheckpoint,
    ) -> FlowCheckpoint:
        async with self._lock:
            attempt = self._owned_attempt(lease)
            now = self._now()
            if attempt.target_kind is not ExecutionTargetKind.FLOW_RUN:
                raise ExecutionLeaseLostError(
                    "an Agent Attempt cannot write a Flow checkpoint"
                )
            if attempt.lease_expires_at <= now:
                raise ExecutionLeaseLostError(
                    f"Attempt lease '{lease.attempt_id}' expired"
                )
            if checkpoint.flow_run_id != attempt.target_id:
                raise ValueError("Flow checkpoint targets another Flow Run")
            store = self._require_flow_store()
            target = await store.get(attempt.target_id)
            if (
                target.status is not FlowStatus.RUNNING
                or target.current_attempt_id != attempt.attempt_id
            ):
                raise ExecutionLeaseLostError(
                    "Attempt is no longer current for the Flow Run"
                )
            if (
                target.flow_id != checkpoint.flow_id
                or target.flow_kind is not checkpoint.flow_kind
                or target.definition_fingerprint
                != checkpoint.definition_fingerprint
            ):
                raise FlowTransitionError(
                    "Flow checkpoint does not match its pinned Flow definition"
                )
            saved = await store.save(
                checkpoint,
                expected_revision=target.checkpoint_revision,
            )
            await store.transition(
                target.flow_run_id,
                expected_statuses={FlowStatus.RUNNING},
                expected_revision=target.revision,
                status=FlowStatus.RUNNING,
                updates={
                    "checkpoint_revision": saved.revision,
                    "checkpoint_stage": saved.stage,
                    "plan_revision": saved.plan_revision,
                    "waiting_child_run_id": saved.waiting_child_run_id,
                },
            )
            return saved

    async def load_flow_checkpoint(self, flow_run_id: str) -> FlowCheckpoint:
        return await self._require_flow_store().load(flow_run_id)

    async def _emit_cancelled(
        self,
        record: AgentRunRecord | FlowRunRecord,
    ) -> None:
        if self._event_store is not None:
            target_id = (
                record.run_id
                if isinstance(record, AgentRunRecord)
                else record.flow_run_id
            )
            await self._event_store.emit(
                target_id,
                (
                    "run.cancelled"
                    if isinstance(record, AgentRunRecord)
                    else "flow.cancelled"
                ),
                {"error": record.error, "source": "execution_store"},
            )

    async def _recover_attempt(
        self,
        attempt: ExecutionAttempt,
        *,
        attempt_status: ExecutionAttemptStatus,
        reason_code: str,
    ) -> AgentRunRecord | FlowRunRecord:
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
        target = await self._target(attempt.target_kind, attempt.target_id)
        if (
            target.current_attempt_id != attempt.attempt_id
            or target.status.value != "running"
        ):
            return target
        recovery = await self._transition_target(
            target,
            status="recovery_required",
            updates={
                "current_attempt_id": None,
                "recovery_reason_code": reason_code,
                "recovery_detected_at": now,
                "error": "Execution ownership was lost; Operator decision is required",
            },
        )
        if self._event_store is not None:
            await self._event_store.emit(
                attempt.target_id,
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
                attempt.target_id,
                (
                    "run.recovery_required"
                    if attempt.target_kind is ExecutionTargetKind.AGENT_RUN
                    else "flow.recovery_required"
                ),
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
            or attempt.request_id != lease.request_id
            or attempt.target_id != lease.target_id
        ):
            raise ExecutionLeaseLostError(f"Attempt lease '{lease.attempt_id}' is not owned")
        return attempt

    def _request_by_key(self, key: str) -> ExecutionRequest | None:
        return next((item for item in self._requests.values() if item.idempotency_key == key), None)

    def _original_execute_request(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind,
    ) -> ExecutionRequest:
        request = next(
            (
                item
                for item in self._requests.values()
                if item.target_kind is target_kind
                and item.target_id == target_id
                and item.operation is ExecutionOperation.EXECUTE
            ),
            None,
        )
        if request is None:
            raise ExecutionRequestNotFoundError(
                f"original execute request for target '{target_id}' was not found"
            )
        return request

    async def _target(
        self,
        target_kind: ExecutionTargetKind,
        target_id: str,
    ) -> AgentRunRecord | FlowRunRecord:
        if target_kind is ExecutionTargetKind.AGENT_RUN:
            return await self.run_store.get(target_id)
        return await self._require_flow_store().get(target_id)

    async def _transition_target(
        self,
        target: AgentRunRecord | FlowRunRecord,
        *,
        status: str,
        updates: Mapping[str, Any],
    ) -> AgentRunRecord | FlowRunRecord:
        if isinstance(target, AgentRunRecord):
            return await self.run_store.transition(
                target.run_id,
                expected_statuses={target.status},
                expected_revision=target.revision,
                status=RunStatus(status),
                updates=_agent_updates(updates),
            )
        return await self._require_flow_store().transition(
            target.flow_run_id,
            expected_statuses={target.status},
            expected_revision=target.revision,
            status=FlowStatus(status),
            updates=_flow_updates(updates),
        )

    def _require_flow_store(self) -> MutableFlowRunStore:
        if self.flow_store is None:
            raise RuntimeError("Flow execution requires a FlowRunStore")
        return self.flow_store

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


def _target_terminal_event_kind(
    target_kind: ExecutionTargetKind,
    status: str,
) -> str:
    prefix = "run" if target_kind is ExecutionTargetKind.AGENT_RUN else "flow"
    if status not in {"waiting", "succeeded", "failed", "cancelled"}:
        raise ValueError(f"unsupported terminal execution status '{status}'")
    return f"{prefix}.{status}"


def _target_terminal_event_payload(
    attempt_id: str | None,
    *,
    target_kind: ExecutionTargetKind,
    status: str,
    output: Any | None,
    text: str | None,
    error: str | None,
    checkpoint_revision: int | None,
    pending_input: Mapping[str, Any] | None = None,
    waiting_child_run_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if attempt_id is not None:
        payload["execution_attempt_id"] = attempt_id
    if status == "succeeded":
        if target_kind is ExecutionTargetKind.AGENT_RUN:
            payload.update(output=_json_value(output), text=text or "")
        else:
            payload["output"] = text if text is not None else _flow_output(output)
    elif status in {"failed", "cancelled"}:
        payload["error"] = error
        if target_kind is ExecutionTargetKind.AGENT_RUN:
            payload["text"] = text or ""
    else:
        payload["checkpoint_revision"] = checkpoint_revision
        if pending_input is not None:
            normalized = _json_mapping(pending_input)
            input_kind = normalized.pop("kind", None)
            payload.update(normalized)
            payload["input_kind"] = input_kind
        if waiting_child_run_id is not None:
            payload["agent_run_id"] = waiting_child_run_id
    return payload


def _agent_updates(updates: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {"checkpoint_stage", "plan_revision", "waiting_child_run_id"}
    return {key: value for key, value in updates.items() if key not in excluded}


def _flow_updates(updates: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in updates.items() if key != "text"}


def _flow_output(value: Any | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        output = value.get("output")
        return output if isinstance(output, str) else None
    raise ValueError("Flow output must be a string or an object containing output")


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
