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

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

from base_agent.artifacts import Artifact
from base_agent.events import EventStore
from base_agent.flow_store import (
    FlowCheckpoint,
    FlowCheckpointNotFoundError,
    FlowCheckpointStore,
    FlowRunRecord,
    FlowRunStore,
    FlowTransitionError,
)
from base_agent.flows.models import FlowStatus
from base_agent.governance import SideEffect
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


class ArtifactReceiptStatus(StrEnum):
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class SideEffectStatus(StrEnum):
    STARTED = "started"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


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


class RecoveryGateBlockedError(ExecutionRequestConflictError):
    """Persisted evidence proves that the requested replay is not safe."""


class ArtifactReceipt(BaseModel):
    """Fenced, immutable Artifact metadata plus Host verification state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    target_kind: ExecutionTargetKind
    target_id: str = Field(min_length=1)
    execution_attempt_id: str = Field(min_length=1)
    fencing_token: str = Field(min_length=1)
    name: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    producer_run_id: str = Field(min_length=1)
    producer_agent_id: str = Field(min_length=1)
    flow_run_id: str | None = None
    invocation_id: str | None = None
    stage: str | None = None
    step_id: str | None = None
    stage_attempt: int | None = Field(default=None, ge=1)
    tool_call_id: str | None = None
    location: str | None = None
    source_artifact_ids: tuple[str, ...] = ()
    status: ArtifactReceiptStatus = ArtifactReceiptStatus.PENDING
    checkpoint_revision: int | None = Field(default=None, ge=1)
    verification_detail: str | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    verified_at: datetime | None = None

    @field_validator("recorded_at", "verified_at")
    @classmethod
    def receipt_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Artifact receipt timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def verified_receipt_has_checkpoint(self) -> ArtifactReceipt:
        if self.status is ArtifactReceiptStatus.VERIFIED:
            if self.checkpoint_revision is None or self.verified_at is None:
                raise ValueError(
                    "verified Artifact receipt requires checkpoint revision and time"
                )
        if len(set(self.source_artifact_ids)) != len(self.source_artifact_ids):
            raise ValueError("Artifact receipt source ids must be unique")
        return self


class SideEffectEvidence(BaseModel):
    """Fenced Tool operation evidence used to decide whether replay is safe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str = Field(min_length=1)
    target_kind: ExecutionTargetKind
    target_id: str = Field(min_length=1)
    execution_attempt_id: str = Field(min_length=1)
    fencing_token: str = Field(min_length=1)
    operation_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)
    intent_sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    side_effect: SideEffect
    status: SideEffectStatus
    result_status: str | None = None
    stage: str | None = None
    step_id: str | None = None
    stage_attempt: int | None = Field(default=None, ge=1)
    turn: int | None = Field(default=None, ge=1)
    checkpoint_revision: int | None = Field(default=None, ge=1)
    resolved_by_attempt_id: str | None = None
    resolution_fencing_token: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    updated_at: datetime

    @field_validator("started_at", "completed_at", "updated_at")
    @classmethod
    def evidence_timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("side-effect evidence timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def evidence_lifecycle_is_consistent(self) -> SideEffectEvidence:
        if self.updated_at < self.started_at:
            raise ValueError("side-effect evidence cannot update before it starts")
        if self.completed_at is not None and self.completed_at < self.started_at:
            raise ValueError("side-effect evidence cannot complete before it starts")
        return self


class RecoveryGateBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    detail: str = Field(min_length=1)
    artifact_id: str | None = None
    evidence_id: str | None = None
    operation_id: str | None = None


class RecoveryEvidenceSnapshot(BaseModel):
    """Operator-visible evidence and deterministic blockers per recovery strategy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_kind: ExecutionTargetKind
    target_id: str = Field(min_length=1)
    checkpoint_revision: int | None = Field(default=None, ge=1)
    checkpoint_stage: str | None = None
    artifact_receipts: tuple[ArtifactReceipt, ...] = ()
    side_effect_evidence: tuple[SideEffectEvidence, ...] = ()
    strategy_blockers: dict[RecoveryStrategy, tuple[RecoveryGateBlocker, ...]]

    def blockers_for(self, strategy: RecoveryStrategy) -> tuple[RecoveryGateBlocker, ...]:
        return self.strategy_blockers.get(strategy, ())


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

    async def record_event_evidence(
        self,
        lease: ExecutionLease,
        kind: str,
        payload: Mapping[str, Any],
        *,
        turn: int | None = None,
    ) -> None: ...

    async def recovery_evidence(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> RecoveryEvidenceSnapshot: ...

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
        self._artifact_receipts: dict[str, ArtifactReceipt] = {}
        self._side_effect_evidence: dict[str, SideEffectEvidence] = {}
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
            if status.value in {"failed", "cancelled"}:
                self._mark_open_side_effects_unknown(attempt.attempt_id, now=now)
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
            self._mark_open_side_effects_unknown(attempt.attempt_id, now=now)
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

    async def record_event_evidence(
        self,
        lease: ExecutionLease,
        kind: str,
        payload: Mapping[str, Any],
        *,
        turn: int | None = None,
    ) -> None:
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
            self._record_event_evidence_unlocked(
                attempt,
                kind,
                payload,
                turn=turn,
                now=now,
            )

    async def recovery_evidence(
        self,
        target_id: str,
        *,
        target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN,
    ) -> RecoveryEvidenceSnapshot:
        async with self._lock:
            target = await self._target(target_kind, target_id)
            return await self._recovery_evidence_unlocked(target)

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
                assert strategy is not None
                evidence = await self._recovery_evidence_unlocked(target)
                blockers = evidence.blockers_for(strategy)
                if blockers:
                    raise RecoveryGateBlockedError(
                        _recovery_gate_error(strategy, blockers)
                    )
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
                payload["recovery_strategy"] = strategy.value
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
            receipt_updates = self._checkpoint_receipt_updates(
                attempt,
                checkpoint,
                now=now,
            )
            saved = await store.save(
                checkpoint,
                expected_revision=target.checkpoint_revision,
            )
            self._artifact_receipts.update(
                {receipt.artifact_id: receipt for receipt in receipt_updates}
            )
            self._mark_attempt_evidence_checkpointed(
                attempt.attempt_id,
                checkpoint_revision=saved.revision,
                now=now,
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

    def _record_event_evidence_unlocked(
        self,
        attempt: ExecutionAttempt,
        kind: str,
        payload: Mapping[str, Any],
        *,
        turn: int | None,
        now: datetime,
    ) -> None:
        if kind == "artifact.created":
            receipt = _artifact_receipt_from_event(attempt, payload, now=now)
            existing_receipt = self._artifact_receipts.get(receipt.artifact_id)
            if existing_receipt is not None:
                _validate_artifact_receipt_intent(existing_receipt, receipt)
                return
            self._artifact_receipts[receipt.artifact_id] = receipt
            return
        if kind == "tool.started":
            evidence = _side_effect_started_from_event(
                attempt,
                payload,
                turn=turn,
                now=now,
            )
            for existing_evidence in self._side_effect_evidence.values():
                if (
                    existing_evidence.target_kind is attempt.target_kind
                    and existing_evidence.target_id == attempt.target_id
                    and existing_evidence.operation_id == evidence.operation_id
                ):
                    _validate_side_effect_intent(existing_evidence, evidence)
            matching_evidence = self._side_effect_evidence.get(evidence.evidence_id)
            if matching_evidence is not None:
                _validate_side_effect_intent(matching_evidence, evidence)
                return
            self._side_effect_evidence[evidence.evidence_id] = evidence
            return
        if kind in {"tool.waiting", "tool.completed"}:
            operation_id = _optional_payload_string(payload, "operation_id")
            if operation_id is None:
                return
            evidence_id = _side_effect_evidence_id(operation_id, attempt.attempt_id)
            started_evidence = self._side_effect_evidence.get(evidence_id)
            if started_evidence is None:
                if kind != "tool.completed":
                    raise ExecutionRequestConflictError(
                        f"Tool operation '{operation_id}' has no current started evidence"
                    )
                started_evidence = _resumable_side_effect_evidence(
                    self._side_effect_evidence.values(),
                    attempt,
                    operation_id,
                )
            status, result_status = _side_effect_completion_status(kind, payload)
            self._side_effect_evidence[started_evidence.evidence_id] = (
                started_evidence.model_copy(
                update={
                    "status": status,
                    "result_status": result_status,
                    "completed_at": (now if kind == "tool.completed" else None),
                    "resolved_by_attempt_id": (
                        attempt.attempt_id
                        if started_evidence.execution_attempt_id != attempt.attempt_id
                        else started_evidence.resolved_by_attempt_id
                    ),
                    "resolution_fencing_token": (
                        attempt.fencing_token
                        if started_evidence.execution_attempt_id != attempt.attempt_id
                        else started_evidence.resolution_fencing_token
                    ),
                    "updated_at": now,
                }
                )
            )
            return
        if kind in {"run.waiting", "execution.child.waiting"}:
            checkpoint_revision = payload.get("checkpoint_revision")
            if isinstance(checkpoint_revision, int) and not isinstance(
                checkpoint_revision, bool
            ):
                self._mark_attempt_evidence_checkpointed(
                    attempt.attempt_id,
                    checkpoint_revision=checkpoint_revision,
                    now=now,
                )

    def _checkpoint_receipt_updates(
        self,
        attempt: ExecutionAttempt,
        checkpoint: FlowCheckpoint,
        *,
        now: datetime,
    ) -> tuple[ArtifactReceipt, ...]:
        artifacts = _checkpoint_artifacts(checkpoint)
        if {artifact.id for artifact in artifacts} != set(checkpoint.artifact_ids):
            raise FlowTransitionError(
                "Flow checkpoint Artifact ids do not match its verified Artifact metadata"
            )
        updates: list[ArtifactReceipt] = []
        for artifact in artifacts:
            if artifact.flow_run_id != attempt.target_id:
                raise FlowTransitionError(
                    f"Artifact '{artifact.id}' does not belong to the Flow Run"
                )
            pending = _artifact_receipt_from_artifact(attempt, artifact, now=now)
            existing = self._artifact_receipts.get(artifact.id)
            if existing is not None:
                _validate_artifact_receipt_intent(existing, pending)
                pending = existing
            updates.append(
                pending.model_copy(
                    update={
                        "status": ArtifactReceiptStatus.VERIFIED,
                        "checkpoint_revision": (
                            pending.checkpoint_revision or checkpoint.revision
                        ),
                        "verification_detail": "host_checkpoint",
                        "verified_at": pending.verified_at or now,
                    }
                )
            )
        return tuple(updates)

    def _mark_attempt_evidence_checkpointed(
        self,
        attempt_id: str,
        *,
        checkpoint_revision: int,
        now: datetime,
    ) -> None:
        for evidence_id, evidence in tuple(self._side_effect_evidence.items()):
            if (
                evidence.execution_attempt_id == attempt_id
                and evidence.checkpoint_revision is None
            ):
                self._side_effect_evidence[evidence_id] = evidence.model_copy(
                    update={
                        "checkpoint_revision": checkpoint_revision,
                        "updated_at": now,
                    }
                )

    def _mark_open_side_effects_unknown(
        self,
        attempt_id: str,
        *,
        now: datetime,
    ) -> None:
        for evidence_id, evidence in tuple(self._side_effect_evidence.items()):
            if (
                evidence.execution_attempt_id == attempt_id
                and evidence.status
                in {SideEffectStatus.STARTED, SideEffectStatus.WAITING}
            ):
                self._side_effect_evidence[evidence_id] = evidence.model_copy(
                    update={
                        "status": SideEffectStatus.UNKNOWN,
                        "result_status": "execution_interrupted",
                        "completed_at": now,
                        "updated_at": now,
                    }
                )

    async def _recovery_evidence_unlocked(
        self,
        target: AgentRunRecord | FlowRunRecord,
    ) -> RecoveryEvidenceSnapshot:
        target_kind = (
            ExecutionTargetKind.AGENT_RUN
            if isinstance(target, AgentRunRecord)
            else ExecutionTargetKind.FLOW_RUN
        )
        target_id = target.run_id if isinstance(target, AgentRunRecord) else target.flow_run_id
        receipts = tuple(
            sorted(
                (
                    receipt.model_copy(deep=True)
                    for receipt in self._artifact_receipts.values()
                    if receipt.target_kind is target_kind
                    and receipt.target_id == target_id
                ),
                key=lambda receipt: (receipt.recorded_at, receipt.artifact_id),
            )
        )
        effects = tuple(
            sorted(
                (
                    evidence.model_copy(deep=True)
                    for evidence in self._side_effect_evidence.values()
                    if evidence.target_kind is target_kind
                    and evidence.target_id == target_id
                ),
                key=lambda evidence: (evidence.started_at, evidence.evidence_id),
            )
        )
        checkpoint: FlowCheckpoint | None = None
        checkpoint_error: str | None = None
        if isinstance(target, FlowRunRecord) and target.checkpoint_revision is not None:
            try:
                checkpoint = await self._require_flow_store().load(target_id)
            except FlowCheckpointNotFoundError:
                checkpoint_error = "durable Flow checkpoint was not found"
            else:
                if checkpoint.revision != target.checkpoint_revision:
                    checkpoint_error = (
                        "Flow checkpoint revision does not match the logical Flow Run"
                    )
        blockers = _recovery_strategy_blockers(
            target,
            receipts=receipts,
            side_effects=effects,
            checkpoint=checkpoint,
            checkpoint_error=checkpoint_error,
        )
        return RecoveryEvidenceSnapshot(
            target_kind=target_kind,
            target_id=target_id,
            checkpoint_revision=target.checkpoint_revision,
            checkpoint_stage=(
                target.checkpoint_stage if isinstance(target, FlowRunRecord) else None
            ),
            artifact_receipts=receipts,
            side_effect_evidence=effects,
            strategy_blockers=blockers,
        )

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
        self._mark_open_side_effects_unknown(attempt.attempt_id, now=now)
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


def _artifact_receipt_from_event(
    attempt: ExecutionAttempt,
    payload: Mapping[str, Any],
    *,
    now: datetime,
) -> ArtifactReceipt:
    flow_run_id = _optional_payload_string(payload, "flow_run_id")
    if (
        attempt.target_kind is ExecutionTargetKind.FLOW_RUN
        and flow_run_id != attempt.target_id
    ):
        raise ExecutionRequestConflictError(
            "Artifact receipt does not belong to the fenced Flow Run"
        )
    return ArtifactReceipt(
        artifact_id=_required_payload_string(payload, "artifact_id"),
        target_kind=attempt.target_kind,
        target_id=attempt.target_id,
        execution_attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        name=_required_payload_string(payload, "name"),
        media_type=_required_payload_string(payload, "media_type"),
        size_bytes=_required_payload_int(payload, "size_bytes", minimum=0),
        sha256=_required_payload_string(payload, "sha256"),
        producer_run_id=_required_payload_string(payload, "producer_run_id"),
        producer_agent_id=_required_payload_string(payload, "producer_agent_id"),
        flow_run_id=flow_run_id,
        invocation_id=_optional_payload_string(payload, "invocation_id"),
        stage=_optional_payload_string(payload, "stage"),
        step_id=_optional_payload_string(payload, "step_id"),
        stage_attempt=_optional_payload_int(payload, "attempt", minimum=1),
        tool_call_id=_optional_payload_string(payload, "tool_call_id"),
        location=_optional_payload_string(payload, "location"),
        source_artifact_ids=_payload_string_tuple(payload, "source_artifact_ids"),
        recorded_at=now,
    )


def _artifact_receipt_from_artifact(
    attempt: ExecutionAttempt,
    artifact: Artifact,
    *,
    now: datetime,
) -> ArtifactReceipt:
    return ArtifactReceipt(
        artifact_id=artifact.id,
        target_kind=attempt.target_kind,
        target_id=attempt.target_id,
        execution_attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        name=artifact.name,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        producer_run_id=artifact.run_id,
        producer_agent_id=artifact.agent_id,
        flow_run_id=artifact.flow_run_id,
        invocation_id=artifact.invocation_id,
        stage=artifact.stage,
        step_id=artifact.step_id,
        stage_attempt=artifact.attempt,
        tool_call_id=artifact.tool_call_id,
        location=artifact.location,
        source_artifact_ids=artifact.source_artifact_ids,
        recorded_at=now,
    )


def _validate_artifact_receipt_intent(
    existing: ArtifactReceipt,
    proposed: ArtifactReceipt,
) -> None:
    immutable_fields = (
        "target_kind",
        "target_id",
        "artifact_id",
        "name",
        "media_type",
        "size_bytes",
        "sha256",
        "producer_run_id",
        "producer_agent_id",
        "flow_run_id",
        "invocation_id",
        "stage",
        "step_id",
        "stage_attempt",
        "tool_call_id",
        "location",
        "source_artifact_ids",
    )
    if any(getattr(existing, field) != getattr(proposed, field) for field in immutable_fields):
        raise ExecutionRequestConflictError(
            f"Artifact receipt '{proposed.artifact_id}' changed immutable metadata"
        )


def _side_effect_started_from_event(
    attempt: ExecutionAttempt,
    payload: Mapping[str, Any],
    *,
    turn: int | None,
    now: datetime,
) -> SideEffectEvidence:
    operation_id = _required_payload_string(payload, "operation_id")
    idempotency_key = _required_payload_string(payload, "idempotency_key")
    if operation_id != idempotency_key:
        raise ExecutionRequestConflictError(
            "Tool operation_id and idempotency_key must identify the same intent"
        )
    return SideEffectEvidence(
        evidence_id=_side_effect_evidence_id(operation_id, attempt.attempt_id),
        target_kind=attempt.target_kind,
        target_id=attempt.target_id,
        execution_attempt_id=attempt.attempt_id,
        fencing_token=attempt.fencing_token,
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        intent_sha256=_required_payload_string(payload, "intent_sha256"),
        tool_call_id=_required_payload_string(payload, "tool_call_id"),
        tool_name=_required_payload_string(payload, "tool_name"),
        side_effect=SideEffect(_required_payload_string(payload, "side_effect")),
        status=SideEffectStatus.STARTED,
        stage=_optional_payload_string(payload, "stage"),
        step_id=_optional_payload_string(payload, "step_id"),
        stage_attempt=_optional_payload_int(payload, "attempt", minimum=1),
        turn=turn,
        started_at=now,
        updated_at=now,
    )


def _validate_side_effect_intent(
    existing: SideEffectEvidence,
    proposed: SideEffectEvidence,
) -> None:
    if (
        existing.target_kind is not proposed.target_kind
        or existing.target_id != proposed.target_id
        or existing.operation_id != proposed.operation_id
        or existing.idempotency_key != proposed.idempotency_key
        or existing.intent_sha256 != proposed.intent_sha256
        or existing.tool_name != proposed.tool_name
        or existing.side_effect is not proposed.side_effect
    ):
        raise ExecutionRequestConflictError(
            f"Tool operation '{proposed.operation_id}' changed intent"
        )


def _resumable_side_effect_evidence(
    evidence_records: Collection[SideEffectEvidence],
    attempt: ExecutionAttempt,
    operation_id: str,
) -> SideEffectEvidence:
    candidates = [
        evidence
        for evidence in evidence_records
        if evidence.target_kind is attempt.target_kind
        and evidence.target_id == attempt.target_id
        and evidence.operation_id == operation_id
        and evidence.checkpoint_revision is not None
        and (
            evidence.status is SideEffectStatus.WAITING
            or evidence.resolved_by_attempt_id == attempt.attempt_id
        )
    ]
    if not candidates:
        raise ExecutionRequestConflictError(
            f"Tool operation '{operation_id}' completed without resumable evidence"
        )
    return max(candidates, key=lambda evidence: evidence.updated_at)


def _side_effect_completion_status(
    kind: str,
    payload: Mapping[str, Any],
) -> tuple[SideEffectStatus, str | None]:
    if kind == "tool.waiting":
        return SideEffectStatus.WAITING, "waiting"
    result_status = _optional_payload_string(payload, "status")
    if result_status == "success":
        return SideEffectStatus.SUCCEEDED, result_status
    if result_status == "cancelled":
        return SideEffectStatus.CANCELLED, result_status
    if result_status in {"timeout", "error"}:
        return SideEffectStatus.UNKNOWN, result_status
    return SideEffectStatus.FAILED, result_status


def _side_effect_evidence_id(operation_id: str, attempt_id: str) -> str:
    digest = hashlib.sha256(f"{operation_id}:{attempt_id}".encode()).hexdigest()
    return f"effect:{digest}"


def _checkpoint_artifacts(checkpoint: FlowCheckpoint) -> tuple[Artifact, ...]:
    key = (
        "accepted_artifacts"
        if checkpoint.flow_kind.value == "sequential"
        else "selected_artifacts"
    )
    payload = checkpoint.cursor.get(key, [])
    if not isinstance(payload, list):
        raise FlowTransitionError(
            f"Flow checkpoint '{key}' must contain verified Artifact metadata"
        )
    adapter = TypeAdapter(Artifact)
    try:
        artifacts = tuple(adapter.validate_python(item) for item in payload)
    except Exception as exc:
        raise FlowTransitionError(
            "Flow checkpoint contains invalid verified Artifact metadata"
        ) from exc
    if len({artifact.id for artifact in artifacts}) != len(artifacts):
        raise FlowTransitionError("Flow checkpoint contains duplicate Artifact ids")
    return artifacts


def _recovery_strategy_blockers(
    target: AgentRunRecord | FlowRunRecord,
    *,
    receipts: tuple[ArtifactReceipt, ...],
    side_effects: tuple[SideEffectEvidence, ...],
    checkpoint: FlowCheckpoint | None,
    checkpoint_error: str | None,
) -> dict[RecoveryStrategy, tuple[RecoveryGateBlocker, ...]]:
    by_artifact_id = {receipt.artifact_id: receipt for receipt in receipts}
    blockers: dict[RecoveryStrategy, tuple[RecoveryGateBlocker, ...]] = {}
    for strategy in RecoveryStrategy:
        current: list[RecoveryGateBlocker] = []
        uses_checkpoint = strategy is not RecoveryStrategy.RESTART
        if uses_checkpoint and target.checkpoint_revision is None:
            current.append(
                RecoveryGateBlocker(
                    code="checkpoint_missing",
                    detail="the recovery strategy requires a durable checkpoint",
                )
            )
        if strategy is RecoveryStrategy.FROM_STAGE:
            if not isinstance(target, FlowRunRecord):
                current.append(
                    RecoveryGateBlocker(
                        code="stage_recovery_unsupported",
                        detail="Agent Runs do not support from_stage recovery",
                    )
                )
            elif target.checkpoint_stage is None:
                current.append(
                    RecoveryGateBlocker(
                        code="checkpoint_stage_missing",
                        detail="the Flow checkpoint has no durable Stage boundary",
                    )
                )
        if uses_checkpoint and checkpoint_error is not None:
            current.append(
                RecoveryGateBlocker(
                    code="checkpoint_invalid",
                    detail=checkpoint_error,
                )
            )
        if uses_checkpoint and checkpoint is not None:
            for artifact_id in checkpoint.artifact_ids:
                receipt = by_artifact_id.get(artifact_id)
                if receipt is None:
                    current.append(
                        RecoveryGateBlocker(
                            code="artifact_receipt_missing",
                            detail=(
                                f"checkpoint Artifact '{artifact_id}' has no persisted receipt"
                            ),
                            artifact_id=artifact_id,
                        )
                    )
                    continue
                if (
                    receipt.status is not ArtifactReceiptStatus.VERIFIED
                    or receipt.checkpoint_revision is None
                    or receipt.checkpoint_revision > checkpoint.revision
                ):
                    current.append(
                        RecoveryGateBlocker(
                            code="artifact_not_verified",
                            detail=(
                                f"checkpoint Artifact '{artifact_id}' is not Host-verified"
                            ),
                            artifact_id=artifact_id,
                        )
                    )
        for evidence in side_effects:
            covered_by_checkpoint = (
                target.checkpoint_revision is not None
                and evidence.checkpoint_revision is not None
                and evidence.checkpoint_revision <= target.checkpoint_revision
            )
            if uses_checkpoint and covered_by_checkpoint:
                continue
            if evidence.side_effect is SideEffect.READ_ONLY:
                continue
            if evidence.side_effect is SideEffect.IDEMPOTENT:
                continue
            current.append(
                RecoveryGateBlocker(
                    code="unsafe_side_effect_replay",
                    detail=(
                        f"{evidence.side_effect.value} Tool '{evidence.tool_name}' "
                        f"has {evidence.status.value} evidence outside the recovery boundary"
                    ),
                    evidence_id=evidence.evidence_id,
                    operation_id=evidence.operation_id,
                )
            )
        blockers[strategy] = tuple(current)
    return blockers


def _recovery_gate_error(
    strategy: RecoveryStrategy,
    blockers: tuple[RecoveryGateBlocker, ...],
) -> str:
    codes = ", ".join(blocker.code for blocker in blockers[:5])
    suffix = "" if len(blockers) <= 5 else f" (+{len(blockers) - 5} more)"
    return f"recovery strategy '{strategy.value}' is blocked by evidence: {codes}{suffix}"


def _required_payload_string(payload: Mapping[str, Any], key: str) -> str:
    value = _optional_payload_string(payload, key)
    if value is None:
        raise ExecutionRequestConflictError(f"evidence payload requires '{key}'")
    return value


def _optional_payload_string(payload: Mapping[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ExecutionRequestConflictError(
            f"evidence payload '{key}' must be a non-blank string"
        )
    return value


def _required_payload_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
) -> int:
    value = _optional_payload_int(payload, key, minimum=minimum)
    if value is None:
        raise ExecutionRequestConflictError(f"evidence payload requires '{key}'")
    return value


def _optional_payload_int(
    payload: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ExecutionRequestConflictError(
            f"evidence payload '{key}' must be an integer >= {minimum}"
        )
    return value


def _payload_string_tuple(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key, ())
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ExecutionRequestConflictError(
            f"evidence payload '{key}' must contain non-blank strings"
        )
    result = tuple(value)
    if len(set(result)) != len(result):
        raise ExecutionRequestConflictError(
            f"evidence payload '{key}' must not contain duplicates"
        )
    return result


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
