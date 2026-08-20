"""Durable Agent Run control-plane records and lease-fenced stores."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from base_agent.models import PendingInputKind, RunStatus

_CLAIMABLE_STATUSES = frozenset({RunStatus.CREATED, RunStatus.WAITING})
_LEASED_STATUS = RunStatus.RUNNING
_SETTLE_STATUSES = frozenset(
    {
        RunStatus.WAITING,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }
)
_TERMINAL_STATUSES = frozenset(
    {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.INTERRUPTED,
    }
)


class RunStoreError(RuntimeError):
    """Base error for persistent Agent Run state operations."""


class RunRecordAlreadyExistsError(RunStoreError):
    """The Run ID has already been registered."""


class RunRecordNotFoundError(RunStoreError, LookupError):
    """No persistent Agent Run record exists for the requested ID."""


class RunLeaseConflictError(RunStoreError):
    """A Run cannot be claimed in its current state."""


class RunLeaseLostError(RunStoreError):
    """The caller no longer owns the current unexpired Run lease."""


class RunTransitionError(RunStoreError):
    """The requested Run state transition is invalid."""


class _RunPendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    kind: PendingInputKind
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRunRecord(BaseModel):
    """Authoritative, JSON-safe lifecycle state for one top-level Agent Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    agent_id: str
    agent_version: str
    snapshot_fingerprint: str
    runtime_build_id: str
    status: RunStatus = RunStatus.CREATED
    revision: int = Field(default=1, ge=1)
    cancel_requested: bool = False
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    checkpoint_revision: int | None = Field(default=None, ge=1)
    output: Any | None = None
    text: str | None = None
    error: str | None = None
    pending_input: dict[str, Any] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "run_id",
        "agent_id",
        "agent_version",
        "snapshot_fingerprint",
        "runtime_build_id",
    )
    @classmethod
    def required_strings_are_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Run identity fields must not be blank")
        return value

    @field_validator("created_at", "updated_at", "lease_expires_at", "heartbeat_at")
    @classmethod
    def timestamps_are_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Run timestamps must be timezone-aware")
        return value

    @field_validator("pending_input", mode="before")
    @classmethod
    def pending_input_has_public_shape(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return _RunPendingInput.model_validate(value).model_dump(mode="json")

    @model_validator(mode="after")
    def lease_matches_status(self) -> AgentRunRecord:
        lease_values = (
            self.lease_owner,
            self.lease_token,
            self.lease_expires_at,
            self.heartbeat_at,
        )
        if self.status is _LEASED_STATUS and any(value is None for value in lease_values):
            raise ValueError("RUNNING records require a complete lease")
        if self.status is not _LEASED_STATUS and any(value is not None for value in lease_values):
            raise ValueError("only RUNNING records may retain a lease")
        if self.updated_at < self.created_at:
            raise ValueError("Run updated_at cannot precede created_at")
        if (
            self.status is RunStatus.RUNNING
            and self.lease_expires_at is not None
            and self.heartbeat_at is not None
            and self.lease_expires_at <= self.heartbeat_at
        ):
            raise ValueError("Run lease must expire after its heartbeat")
        if self.status is RunStatus.WAITING and self.checkpoint_revision is None:
            raise ValueError("WAITING records require checkpoint_revision")
        if self.status is RunStatus.WAITING and self.pending_input is None:
            raise ValueError("WAITING records require pending_input")
        if self.pending_input is not None and self.checkpoint_revision is None:
            raise ValueError("pending_input requires checkpoint_revision")
        if self.status in {
            RunStatus.CREATED,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INTERRUPTED,
        } and (self.checkpoint_revision is not None or self.pending_input is not None):
            raise ValueError(f"{self.status.value.upper()} records cannot retain a checkpoint")
        return self


class RunLease(BaseModel):
    """Opaque fencing token proving temporary ownership of a RUNNING Run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    owner_id: str
    token: str
    expires_at: datetime


@runtime_checkable
class RunStore(Protocol):
    """Persistent state-machine operations for top-level Agent Runs."""

    async def create(self, record: AgentRunRecord) -> AgentRunRecord: ...

    async def create_claimed(
        self,
        record: AgentRunRecord,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> RunLease: ...

    async def get(self, run_id: str) -> AgentRunRecord: ...

    async def claim(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> RunLease: ...

    async def renew(self, lease: RunLease, *, lease_seconds: float) -> RunLease: ...

    async def settle(
        self,
        lease: RunLease,
        *,
        status: RunStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: dict[str, Any] | None = None,
        checkpoint_revision: int | None = None,
    ) -> AgentRunRecord: ...

    async def request_cancel(self, run_id: str) -> AgentRunRecord: ...

    async def cancel_unowned(self, run_id: str, *, reason: str) -> AgentRunRecord: ...

    async def block_expired(
        self,
        *,
        agent_id: str | None = None,
    ) -> tuple[AgentRunRecord, ...]: ...


class InMemoryRunStore:
    """Process-memory RunStore with the same lease and fencing rules as PostgreSQL."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[str, AgentRunRecord] = {}
        self._lock = asyncio.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))

    async def create(self, record: AgentRunRecord) -> AgentRunRecord:
        normalized = _json_record(record)
        async with self._lock:
            if normalized.run_id in self._records:
                raise RunRecordAlreadyExistsError(
                    f"Run record '{normalized.run_id}' already exists"
                )
            self._records[normalized.run_id] = normalized
            return normalized.model_copy(deep=True)

    async def get(self, run_id: str) -> AgentRunRecord:
        async with self._lock:
            return self._get(run_id).model_copy(deep=True)

    async def create_claimed(
        self,
        record: AgentRunRecord,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> RunLease:
        _validate_lease_request(owner_id, lease_seconds)
        normalized = _json_record(record)
        if normalized.status is not RunStatus.CREATED:
            raise RunTransitionError("create_claimed requires a CREATED Run record")
        async with self._lock:
            if normalized.run_id in self._records:
                raise RunRecordAlreadyExistsError(
                    f"Run record '{normalized.run_id}' already exists"
                )
            now = _utc(self._clock())
            lease = RunLease(
                run_id=normalized.run_id,
                owner_id=owner_id,
                token=str(uuid4()),
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._records[normalized.run_id] = normalized.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "revision": normalized.revision + 1,
                    "lease_owner": owner_id,
                    "lease_token": lease.token,
                    "lease_expires_at": lease.expires_at,
                    "heartbeat_at": now,
                    "updated_at": now,
                }
            )
            return lease

    async def claim(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> RunLease:
        _validate_lease_request(owner_id, lease_seconds)
        async with self._lock:
            record = self._get(run_id)
            if record.status not in _CLAIMABLE_STATUSES:
                raise RunLeaseConflictError(
                    f"Run '{run_id}' with status '{record.status.value}' cannot be claimed"
                )
            now = _utc(self._clock())
            lease = RunLease(
                run_id=run_id,
                owner_id=owner_id,
                token=str(uuid4()),
                expires_at=now + timedelta(seconds=lease_seconds),
            )
            self._records[run_id] = record.model_copy(
                update={
                    "status": RunStatus.RUNNING,
                    "revision": record.revision + 1,
                    "lease_owner": lease.owner_id,
                    "lease_token": lease.token,
                    "lease_expires_at": lease.expires_at,
                    "heartbeat_at": now,
                    "updated_at": now,
                    "error": None,
                }
            )
            return lease

    async def renew(self, lease: RunLease, *, lease_seconds: float) -> RunLease:
        _validate_lease_request(lease.owner_id, lease_seconds)
        async with self._lock:
            record = self._owned_record(lease)
            now = _utc(self._clock())
            if record.lease_expires_at is None or record.lease_expires_at <= now:
                raise RunLeaseLostError(f"Run lease for '{lease.run_id}' has expired")
            renewed = lease.model_copy(
                update={"expires_at": now + timedelta(seconds=lease_seconds)}
            )
            self._records[lease.run_id] = record.model_copy(
                update={
                    "revision": record.revision + 1,
                    "lease_expires_at": renewed.expires_at,
                    "heartbeat_at": now,
                    "updated_at": now,
                }
            )
            return renewed

    async def settle(
        self,
        lease: RunLease,
        *,
        status: RunStatus,
        output: Any | None = None,
        text: str | None = None,
        error: str | None = None,
        pending_input: dict[str, Any] | None = None,
        checkpoint_revision: int | None = None,
    ) -> AgentRunRecord:
        _validate_settlement(status, checkpoint_revision)
        async with self._lock:
            record = self._owned_record(lease)
            now = _utc(self._clock())
            if record.lease_expires_at is None or record.lease_expires_at <= now:
                raise RunLeaseLostError(f"Run lease for '{lease.run_id}' has expired")
            settled = record.model_copy(
                update={
                    "status": status,
                    "revision": record.revision + 1,
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "heartbeat_at": None,
                    "checkpoint_revision": checkpoint_revision,
                    "output": _json_value(output),
                    "text": text,
                    "error": error,
                    "pending_input": _json_value(pending_input),
                    "updated_at": now,
                }
            )
            settled = _json_record(settled)
            self._records[lease.run_id] = settled
            return settled.model_copy(deep=True)

    async def request_cancel(self, run_id: str) -> AgentRunRecord:
        async with self._lock:
            record = self._get(run_id)
            if record.status in _TERMINAL_STATUSES:
                return record.model_copy(deep=True)
            if record.cancel_requested:
                return record.model_copy(deep=True)
            updated = record.model_copy(
                update={
                    "cancel_requested": True,
                    "revision": record.revision + 1,
                    "updated_at": _utc(self._clock()),
                }
            )
            self._records[run_id] = updated
            return updated.model_copy(deep=True)

    async def cancel_unowned(self, run_id: str, *, reason: str) -> AgentRunRecord:
        async with self._lock:
            record = self._get(run_id)
            if record.status is RunStatus.CANCELLED:
                return record.model_copy(deep=True)
            if record.status is RunStatus.RUNNING:
                raise RunTransitionError(f"RUNNING Run '{run_id}' must be cancelled by its owner")
            if record.status in _TERMINAL_STATUSES:
                raise RunTransitionError(
                    f"Run '{run_id}' already reached status '{record.status.value}'"
                )
            now = _utc(self._clock())
            cancelled = record.model_copy(
                update={
                    "status": RunStatus.CANCELLED,
                    "revision": record.revision + 1,
                    "cancel_requested": True,
                    "checkpoint_revision": None,
                    "pending_input": None,
                    "output": None,
                    "text": "",
                    "error": reason,
                    "updated_at": now,
                }
            )
            self._records[run_id] = cancelled
            return cancelled.model_copy(deep=True)

    async def block_expired(
        self,
        *,
        agent_id: str | None = None,
    ) -> tuple[AgentRunRecord, ...]:
        async with self._lock:
            now = _utc(self._clock())
            blocked: list[AgentRunRecord] = []
            for run_id, record in tuple(self._records.items()):
                if (
                    record.status is not RunStatus.RUNNING
                    or (agent_id is not None and record.agent_id != agent_id)
                    or record.lease_expires_at is None
                    or record.lease_expires_at > now
                ):
                    continue
                updated = record.model_copy(
                    update={
                        "status": RunStatus.BLOCKED,
                        "revision": record.revision + 1,
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                        "heartbeat_at": None,
                        "error": "Run owner lease expired; automatic replay is disabled",
                        "updated_at": now,
                    }
                )
                self._records[run_id] = updated
                blocked.append(updated.model_copy(deep=True))
            return tuple(blocked)

    def _get(self, run_id: str) -> AgentRunRecord:
        try:
            return self._records[run_id]
        except KeyError as exc:
            raise RunRecordNotFoundError(f"Run record '{run_id}' was not found") from exc

    def _owned_record(self, lease: RunLease) -> AgentRunRecord:
        record = self._get(lease.run_id)
        if (
            record.status is not RunStatus.RUNNING
            or record.lease_owner != lease.owner_id
            or record.lease_token != lease.token
        ):
            raise RunLeaseLostError(f"Run lease for '{lease.run_id}' is no longer owned")
        return record


def run_record_json(record: AgentRunRecord) -> dict[str, Any]:
    """Return one finite JSON payload suitable for PostgreSQL JSONB."""
    return cast(
        dict[str, Any],
        json.loads(
            json.dumps(
                record.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        ),
    )


def _json_record(record: AgentRunRecord) -> AgentRunRecord:
    try:
        return AgentRunRecord.model_validate(run_record_json(record))
    except (TypeError, ValueError) as exc:
        raise ValueError("Run record must contain only finite JSON values") from exc


def _json_value(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Run result values must contain only finite JSON") from exc


def _validate_lease_request(owner_id: str, lease_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Run lease owner_id must not be blank")
    if lease_seconds <= 0:
        raise ValueError("Run lease_seconds must be greater than zero")


def _validate_settlement(status: RunStatus, checkpoint_revision: int | None) -> None:
    if status not in _SETTLE_STATUSES:
        raise RunTransitionError(f"RUNNING Run cannot settle as '{status.value}'")
    if status is RunStatus.WAITING and checkpoint_revision is None:
        raise RunTransitionError("WAITING settlement requires checkpoint_revision")
    if status is not RunStatus.WAITING and checkpoint_revision is not None:
        raise RunTransitionError("only WAITING settlement may retain checkpoint_revision")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("RunStore clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
