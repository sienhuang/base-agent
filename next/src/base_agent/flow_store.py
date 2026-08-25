"""Durable Flow Run records and strategy-owned JSON checkpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Collection, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from base_agent.flow_status import FlowStatus
from base_agent.models import PendingInputKind


class FlowKind(StrEnum):
    SEQUENTIAL = "sequential"
    PLANNER_REACT = "planner_react"


class FlowStoreError(RuntimeError):
    """Base error for durable Flow control-plane state."""


class FlowRunAlreadyExistsError(FlowStoreError):
    """A Flow Run with the same identifier already exists."""


class FlowRunRecordNotFoundError(FlowStoreError, LookupError):
    """No durable Flow Run record exists for the requested identifier."""


class FlowCheckpointNotFoundError(FlowStoreError, LookupError):
    """No durable Flow checkpoint exists for the requested Flow Run."""


class FlowCheckpointConflictError(FlowStoreError):
    """A stale writer attempted to replace a newer Flow checkpoint."""


class FlowTransitionError(FlowStoreError):
    """The requested Flow lifecycle transition is invalid."""


class _FlowPendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    kind: PendingInputKind
    tool_call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FlowRunRecord(BaseModel):
    """Authoritative lifecycle state for one top-level Flow execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flow_run_id: str = Field(min_length=1)
    flow_name: str = Field(min_length=1)
    flow_id: str = Field(min_length=1)
    flow_kind: FlowKind
    definition_fingerprint: str = Field(min_length=1)
    runtime_build_id: str = Field(min_length=1)
    runtime_route: str = Field(default="default", min_length=1)
    status: FlowStatus = FlowStatus.RUNNING
    revision: int = Field(default=1, ge=1)
    cancel_requested: bool = False
    current_request_id: str | None = None
    current_attempt_id: str | None = None
    checkpoint_revision: int | None = Field(default=None, ge=1)
    checkpoint_stage: str | None = None
    plan_revision: int | None = Field(default=None, ge=1)
    waiting_child_run_id: str | None = None
    pending_input: dict[str, Any] | None = None
    output: str | None = None
    error: str | None = None
    recovery_reason_code: str | None = None
    recovery_detected_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("created_at", "updated_at", "recovery_detected_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Flow Run timestamps must be timezone-aware")
        return value

    @field_validator("pending_input", mode="before")
    @classmethod
    def pending_input_has_public_shape(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return _FlowPendingInput.model_validate(value).model_dump(mode="json")

    @model_validator(mode="after")
    def lifecycle_payload_is_consistent(self) -> FlowRunRecord:
        if self.updated_at < self.created_at:
            raise ValueError("Flow Run updated_at cannot precede created_at")
        if self.status is FlowStatus.WAITING:
            if self.checkpoint_revision is None or self.pending_input is None:
                raise ValueError("WAITING Flow Runs require a checkpoint and pending input")
            if self.waiting_child_run_id is None:
                raise ValueError("WAITING Flow Runs require waiting_child_run_id")
        if self.pending_input is not None and self.checkpoint_revision is None:
            raise ValueError("Flow pending_input requires a checkpoint")
        if self.status is FlowStatus.RECOVERY_REQUIRED:
            if self.recovery_reason_code is None or self.recovery_detected_at is None:
                raise ValueError(
                    "RECOVERY_REQUIRED Flow Runs require recovery reason and detection time"
                )
        if self.status in {
            FlowStatus.SUCCEEDED,
            FlowStatus.FAILED,
            FlowStatus.CANCELLED,
        } and (
            self.checkpoint_revision is not None
            or self.pending_input is not None
            or self.waiting_child_run_id is not None
        ):
            raise ValueError("terminal Flow Runs cannot retain a checkpoint or pending input")
        return self


class FlowCheckpoint(BaseModel):
    """Finite strategy state saved at a safe Flow orchestration boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    flow_run_id: str = Field(min_length=1)
    flow_id: str = Field(min_length=1)
    flow_kind: FlowKind
    definition_fingerprint: str = Field(min_length=1)
    revision: int = Field(ge=1)
    cursor: dict[str, Any]
    stage: str | None = None
    plan_revision: int | None = Field(default=None, ge=1)
    waiting_child_run_id: str | None = None
    artifact_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("cursor", mode="before")
    @classmethod
    def cursor_is_finite_json(cls, value: Any) -> dict[str, Any]:
        normalized = _json_value(value)
        if not isinstance(normalized, dict):
            raise ValueError("Flow checkpoint cursor must be a JSON object")
        return normalized

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Flow checkpoint timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def timestamps_are_ordered(self) -> FlowCheckpoint:
        if self.updated_at < self.created_at:
            raise ValueError("Flow checkpoint updated_at cannot precede created_at")
        if any(not artifact_id.strip() for artifact_id in self.artifact_ids):
            raise ValueError("Flow checkpoint artifact_ids must not contain blanks")
        if len(set(self.artifact_ids)) != len(self.artifact_ids):
            raise ValueError("Flow checkpoint artifact_ids must be unique")
        return self


@runtime_checkable
class FlowRunStore(Protocol):
    async def create(self, record: FlowRunRecord) -> FlowRunRecord: ...

    async def get(self, flow_run_id: str) -> FlowRunRecord: ...

    async def transition(
        self,
        flow_run_id: str,
        *,
        expected_statuses: Collection[FlowStatus],
        status: FlowStatus,
        expected_revision: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> FlowRunRecord: ...


@runtime_checkable
class FlowCheckpointStore(Protocol):
    async def save(
        self,
        checkpoint: FlowCheckpoint,
        *,
        expected_revision: int | None,
    ) -> FlowCheckpoint: ...

    async def load(self, flow_run_id: str) -> FlowCheckpoint: ...

    async def delete(self, flow_run_id: str) -> None: ...


class InMemoryFlowRunStore(FlowRunStore, FlowCheckpointStore):
    """Reference Flow state and checkpoint store for tests and local applications."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._records: dict[str, FlowRunRecord] = {}
        self._checkpoints: dict[str, FlowCheckpoint] = {}
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = asyncio.Lock()

    async def create(self, record: FlowRunRecord) -> FlowRunRecord:
        normalized = _flow_record(record)
        async with self._lock:
            if normalized.flow_run_id in self._records:
                raise FlowRunAlreadyExistsError(
                    f"Flow Run '{normalized.flow_run_id}' already exists"
                )
            self._records[normalized.flow_run_id] = normalized
            return normalized.model_copy(deep=True)

    async def get(self, flow_run_id: str) -> FlowRunRecord:
        async with self._lock:
            return self._get(flow_run_id).model_copy(deep=True)

    async def transition(
        self,
        flow_run_id: str,
        *,
        expected_statuses: Collection[FlowStatus],
        status: FlowStatus,
        expected_revision: int | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> FlowRunRecord:
        async with self._lock:
            current = self._get(flow_run_id)
            if current.status not in expected_statuses:
                raise FlowTransitionError(
                    f"Flow Run '{flow_run_id}' with status '{current.status.value}' "
                    f"cannot transition to '{status.value}'"
                )
            if expected_revision is not None and current.revision != expected_revision:
                raise FlowTransitionError(
                    f"Flow Run '{flow_run_id}' revision changed from "
                    f"{expected_revision} to {current.revision}"
                )
            values: dict[str, Any] = {
                "status": status,
                "revision": current.revision + 1,
                "updated_at": self._now(),
                **dict(updates or {}),
            }
            updated = _flow_record(current.model_copy(update=values))
            self._records[flow_run_id] = updated
            return updated.model_copy(deep=True)

    async def save(
        self,
        checkpoint: FlowCheckpoint,
        *,
        expected_revision: int | None,
    ) -> FlowCheckpoint:
        normalized = _flow_checkpoint(checkpoint)
        async with self._lock:
            current = self._checkpoints.get(checkpoint.flow_run_id)
            current_revision = current.revision if current is not None else None
            if current_revision != expected_revision:
                raise FlowCheckpointConflictError(
                    f"Flow checkpoint '{checkpoint.flow_run_id}' revision changed from "
                    f"{expected_revision} to {current_revision}"
                )
            expected_next = 1 if current is None else current.revision + 1
            if normalized.revision != expected_next:
                raise FlowCheckpointConflictError(
                    f"Flow checkpoint revision must advance to {expected_next}"
                )
            if current is not None and (
                current.flow_id != normalized.flow_id
                or current.flow_kind is not normalized.flow_kind
                or current.definition_fingerprint != normalized.definition_fingerprint
            ):
                raise FlowCheckpointConflictError("Flow checkpoint identity changed")
            self._checkpoints[checkpoint.flow_run_id] = normalized
            return normalized.model_copy(deep=True)

    async def load(self, flow_run_id: str) -> FlowCheckpoint:
        async with self._lock:
            try:
                return self._checkpoints[flow_run_id].model_copy(deep=True)
            except KeyError as exc:
                raise FlowCheckpointNotFoundError(
                    f"Flow checkpoint '{flow_run_id}' was not found"
                ) from exc

    async def delete(self, flow_run_id: str) -> None:
        async with self._lock:
            self._checkpoints.pop(flow_run_id, None)

    def _get(self, flow_run_id: str) -> FlowRunRecord:
        try:
            return self._records[flow_run_id]
        except KeyError as exc:
            raise FlowRunRecordNotFoundError(
                f"Flow Run '{flow_run_id}' was not found"
            ) from exc

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("FlowRunStore clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def flow_record_json(record: FlowRunRecord) -> dict[str, Any]:
    return cast(dict[str, Any], _json_value(record.model_dump(mode="json")))


def flow_checkpoint_json(checkpoint: FlowCheckpoint) -> dict[str, Any]:
    return cast(dict[str, Any], _json_value(checkpoint.model_dump(mode="json")))


def _flow_record(record: FlowRunRecord) -> FlowRunRecord:
    return FlowRunRecord.model_validate(flow_record_json(record))


def _flow_checkpoint(checkpoint: FlowCheckpoint) -> FlowCheckpoint:
    return FlowCheckpoint.model_validate(flow_checkpoint_json(checkpoint))


def _json_value(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Flow state must contain only finite JSON values") from exc


__all__ = [
    "FlowCheckpoint",
    "FlowCheckpointConflictError",
    "FlowCheckpointNotFoundError",
    "FlowCheckpointStore",
    "FlowKind",
    "FlowRunAlreadyExistsError",
    "FlowRunRecord",
    "FlowRunRecordNotFoundError",
    "FlowRunStore",
    "FlowStoreError",
    "FlowTransitionError",
    "InMemoryFlowRunStore",
]
