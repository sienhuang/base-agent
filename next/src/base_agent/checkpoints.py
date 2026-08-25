"""Persistent, lease-claimed checkpoints for suspended Agent Runs."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models import PendingInput, PendingInputKind


class CheckpointNotFoundError(LookupError):
    """The requested checkpoint does not exist."""


class CheckpointClaimError(RuntimeError):
    """The checkpoint is already leased by another continuation."""


class CheckpointConflictError(RuntimeError):
    """A stale checkpoint owner attempted to mutate a newer checkpoint."""


class PendingInputCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(min_length=1)
    kind: PendingInputKind
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    operation_id: str | None = Field(default=None, min_length=1, max_length=256)
    operation_index: int | None = Field(default=None, ge=0)

    def public(self) -> PendingInput:
        return PendingInput(
            request_id=self.request_id,
            kind=self.kind,
            tool_call_id=self.tool_call_id,
            tool_name=self.tool_name,
            prompt=self.prompt,
            metadata=self.metadata,
        )


class RuntimeCheckpoint(BaseModel):
    """Finite JSON snapshot needed to resume one exact Agent Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    snapshot_fingerprint: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    stream: bool = False
    invocation: dict[str, Any] = Field(default_factory=dict)
    memory_scope: tuple[str, ...] | None = None
    next_turn: int = Field(ge=1)
    messages: tuple[dict[str, Any], ...]
    memory: dict[str, Any]
    tool_results: tuple[dict[str, Any], ...] = ()
    observation_bytes: int = Field(default=0, ge=0)
    called_any_tool: bool = False
    pending_input: PendingInputCheckpoint
    pending_call: dict[str, Any]
    remaining_calls: tuple[dict[str, Any], ...] = ()
    overflow_calls: tuple[dict[str, Any], ...] = ()
    artifact_ids: tuple[str, ...] = ()
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CheckpointClaim(BaseModel):
    """Fencing token proving ownership of a leased checkpoint revision."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint: RuntimeCheckpoint
    token: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    lease_expires_at: datetime


@runtime_checkable
class CheckpointStore(Protocol):
    async def save(self, checkpoint: RuntimeCheckpoint) -> None: ...

    async def load(self, run_id: str) -> RuntimeCheckpoint: ...

    async def claim(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> CheckpointClaim: ...

    async def release(
        self,
        claim: CheckpointClaim,
        checkpoint: RuntimeCheckpoint,
    ) -> None: ...

    async def renew(
        self,
        claim: CheckpointClaim,
        *,
        lease_seconds: float,
    ) -> CheckpointClaim: ...

    async def complete(self, claim: CheckpointClaim) -> None: ...

    async def delete(self, run_id: str) -> None: ...


class _MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint: RuntimeCheckpoint
    lease_token: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None


class InMemoryCheckpointStore:
    """Process-local checkpoint store with expiring lease claims."""

    def __init__(self) -> None:
        self._records: dict[str, _MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def save(self, checkpoint: RuntimeCheckpoint) -> None:
        async with self._lock:
            if checkpoint.run_id in self._records:
                raise CheckpointConflictError(
                    f"checkpoint for run '{checkpoint.run_id}' already exists"
                )
            self._records[checkpoint.run_id] = _MemoryRecord(checkpoint=checkpoint)

    async def load(self, run_id: str) -> RuntimeCheckpoint:
        async with self._lock:
            try:
                return self._records[run_id].checkpoint.model_copy(deep=True)
            except KeyError as exc:
                raise CheckpointNotFoundError(
                    f"checkpoint for run '{run_id}' was not found"
                ) from exc

    async def claim(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: float,
    ) -> CheckpointClaim:
        _validate_claim(owner_id, lease_seconds)
        now = datetime.now(UTC)
        async with self._lock:
            try:
                record = self._records[run_id]
            except KeyError as exc:
                raise CheckpointNotFoundError(
                    f"checkpoint for run '{run_id}' was not found"
                ) from exc
            if record.lease_expires_at is not None and record.lease_expires_at > now:
                raise CheckpointClaimError(
                    f"checkpoint for run '{run_id}' is already being resumed"
                )
            token = str(uuid4())
            expires_at = now + timedelta(seconds=lease_seconds)
            self._records[run_id] = record.model_copy(
                update={
                    "lease_token": token,
                    "lease_owner": owner_id,
                    "lease_expires_at": expires_at,
                }
            )
            return CheckpointClaim(
                checkpoint=record.checkpoint.model_copy(deep=True),
                token=token,
                owner_id=owner_id,
                lease_expires_at=expires_at,
            )

    async def release(
        self,
        claim: CheckpointClaim,
        checkpoint: RuntimeCheckpoint,
    ) -> None:
        if claim.checkpoint.run_id != checkpoint.run_id:
            raise ValueError("claim and checkpoint run_id must match")
        async with self._lock:
            record = self._require_claim(claim)
            self._records[checkpoint.run_id] = record.model_copy(
                update={
                    "checkpoint": checkpoint,
                    "lease_token": None,
                    "lease_owner": None,
                    "lease_expires_at": None,
                }
            )

    async def renew(
        self,
        claim: CheckpointClaim,
        *,
        lease_seconds: float,
    ) -> CheckpointClaim:
        _validate_claim(claim.owner_id, lease_seconds)
        expires_at = datetime.now(UTC) + timedelta(seconds=lease_seconds)
        async with self._lock:
            record = self._require_claim(claim)
            self._records[claim.checkpoint.run_id] = record.model_copy(
                update={"lease_expires_at": expires_at}
            )
        return claim.model_copy(update={"lease_expires_at": expires_at})

    async def complete(self, claim: CheckpointClaim) -> None:
        async with self._lock:
            self._require_claim(claim)
            del self._records[claim.checkpoint.run_id]

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            self._records.pop(run_id, None)

    def _require_claim(self, claim: CheckpointClaim) -> _MemoryRecord:
        try:
            record = self._records[claim.checkpoint.run_id]
        except KeyError as exc:
            raise CheckpointConflictError("checkpoint claim is no longer current") from exc
        if record.lease_token != claim.token:
            raise CheckpointConflictError("checkpoint claim is no longer current")
        return record


def checkpoint_json(checkpoint: RuntimeCheckpoint) -> dict[str, Any]:
    """Return strict finite JSON for persistence adapters."""
    value = json.loads(checkpoint.model_dump_json())
    if not isinstance(value, dict):
        raise ValueError("checkpoint must serialize to a JSON object")
    return value


def _validate_claim(owner_id: str, lease_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("checkpoint owner_id must not be blank")
    if lease_seconds <= 0:
        raise ValueError("checkpoint lease_seconds must be greater than zero")


__all__ = [
    "CheckpointClaim",
    "CheckpointClaimError",
    "CheckpointConflictError",
    "CheckpointNotFoundError",
    "CheckpointStore",
    "InMemoryCheckpointStore",
    "PendingInputCheckpoint",
    "RuntimeCheckpoint",
]
