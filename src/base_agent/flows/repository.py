"""Atomic persistence boundary for Flow aggregate snapshots and events."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from base_agent.flows.lease import (
    FlowExecutionLease,
    FlowLeaseLostError,
    FlowLeaseUnavailableError,
)
from base_agent.flows.lifecycle import FlowRunState
from base_agent.models import EventType, RunStatus, RuntimeEvent
from base_agent.models.run import utc_now

_TERMINAL_FLOW_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
    RunStatus.LIMIT_REACHED,
}


class FlowRunAlreadyExistsError(RuntimeError):
    """Raised when creating a Flow Run whose identifier is already stored."""


class FlowRunNotFoundError(KeyError):
    """Raised when a Flow Run cannot be found."""


class FlowRevisionConflictError(RuntimeError):
    """Raised when a stale Flow aggregate tries to overwrite newer state."""


class FlowEventDraft(BaseModel):
    """An event without repository-assigned identity, ordering, or timestamp."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: EventType
    data: dict[str, JsonValue] = Field(default_factory=dict)


class FlowRepository(Protocol):
    """Atomically stores a Flow snapshot and its ordered lifecycle facts."""

    async def create(
        self,
        state: FlowRunState,
        *,
        events: tuple[FlowEventDraft, ...],
    ) -> tuple[RuntimeEvent, ...]: ...

    async def get(self, run_id: UUID) -> FlowRunState: ...

    async def commit(
        self,
        state: FlowRunState,
        *,
        expected_revision: int,
        events: tuple[FlowEventDraft, ...],
        execution_token: UUID | None = None,
    ) -> tuple[RuntimeEvent, ...]: ...

    async def append_events(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        events: tuple[FlowEventDraft, ...],
        execution_token: UUID | None = None,
    ) -> tuple[RuntimeEvent, ...]: ...

    async def events(self, run_id: UUID) -> tuple[RuntimeEvent, ...]: ...


class InMemoryFlowRepository:
    """Process-local atomic repository for tests and single-process applications."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._states: dict[UUID, FlowRunState] = {}
        self._events: dict[UUID, list[RuntimeEvent]] = {}
        self._leases: dict[UUID, tuple[FlowExecutionLease, bool]] = {}
        self._lock = asyncio.Lock()
        self._clock = clock

    async def create(
        self,
        state: FlowRunState,
        *,
        events: tuple[FlowEventDraft, ...],
    ) -> tuple[RuntimeEvent, ...]:
        if state.revision != 1:
            raise ValueError("a new Flow Run must start at revision 1")
        if not events:
            raise ValueError("creating a Flow Run requires at least one event")
        async with self._lock:
            if state.run_id in self._states:
                raise FlowRunAlreadyExistsError(
                    f"Flow Run '{state.run_id}' already exists"
                )
            persisted_events = self._materialize_events(
                state.run_id,
                events,
                start_sequence=1,
            )
            self._states[state.run_id] = _copy_state(state)
            self._events[state.run_id] = list(persisted_events)
            return _copy_events(persisted_events)

    async def get(self, run_id: UUID) -> FlowRunState:
        async with self._lock:
            try:
                return _copy_state(self._states[run_id])
            except KeyError as exc:
                raise FlowRunNotFoundError(f"Flow Run '{run_id}' was not found") from exc

    async def commit(
        self,
        state: FlowRunState,
        *,
        expected_revision: int,
        events: tuple[FlowEventDraft, ...],
        execution_token: UUID | None = None,
    ) -> tuple[RuntimeEvent, ...]:
        if not events:
            raise ValueError("committing a Flow Run requires at least one event")
        async with self._lock:
            try:
                current = self._states[state.run_id]
            except KeyError as exc:
                raise FlowRunNotFoundError(
                    f"Flow Run '{state.run_id}' was not found"
                ) from exc
            self._validate_execution_token(
                state.run_id,
                execution_token,
                now=self._clock(),
            )
            validate_flow_replacement(
                current,
                state,
                expected_revision=expected_revision,
            )
            current_events = self._events[state.run_id]
            persisted_events = self._materialize_events(
                state.run_id,
                events,
                start_sequence=len(current_events) + 1,
            )
            self._states[state.run_id] = _copy_state(state)
            current_events.extend(persisted_events)
            return _copy_events(persisted_events)

    async def events(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        async with self._lock:
            if run_id not in self._states:
                raise FlowRunNotFoundError(f"Flow Run '{run_id}' was not found")
            return _copy_events(tuple(self._events[run_id]))

    async def append_events(
        self,
        run_id: UUID,
        *,
        expected_revision: int,
        events: tuple[FlowEventDraft, ...],
        execution_token: UUID | None = None,
    ) -> tuple[RuntimeEvent, ...]:
        if not events:
            raise ValueError("appending Flow events requires at least one event")
        async with self._lock:
            try:
                current = self._states[run_id]
            except KeyError as exc:
                raise FlowRunNotFoundError(
                    f"Flow Run '{run_id}' was not found"
                ) from exc
            self._validate_execution_token(
                run_id,
                execution_token,
                now=self._clock(),
            )
            if current.revision != expected_revision:
                raise FlowRevisionConflictError(
                    f"Flow Run '{run_id}' revision conflict: "
                    f"expected {expected_revision}, found {current.revision}"
                )
            current_events = self._events[run_id]
            persisted_events = self._materialize_events(
                run_id,
                events,
                start_sequence=len(current_events) + 1,
            )
            current_events.extend(persisted_events)
            return _copy_events(persisted_events)

    async def acquire_execution(
        self,
        run_id: UUID,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowExecutionLease:
        _validate_lease_arguments(owner_id, ttl_seconds)
        acquired_at = now or self._clock()
        async with self._lock:
            try:
                state = self._states[run_id]
            except KeyError as exc:
                raise FlowRunNotFoundError(
                    f"Flow Run '{run_id}' was not found"
                ) from exc
            if state.status in _TERMINAL_FLOW_STATUSES:
                raise FlowLeaseUnavailableError(
                    f"terminal Flow Run '{run_id}' cannot be claimed"
                )
            prior = self._leases.get(run_id)
            if (
                prior is not None
                and prior[1]
                and not prior[0].is_expired(now=acquired_at)
            ):
                raise FlowLeaseUnavailableError(
                    f"Flow Run '{run_id}' is leased by '{prior[0].owner_id}'"
                )
            lease = FlowExecutionLease(
                run_id=run_id,
                token=uuid4(),
                owner_id=owner_id,
                attempt=(prior[0].attempt + 1 if prior is not None else 1),
                acquired_at=acquired_at,
                heartbeat_at=acquired_at,
                expires_at=acquired_at + timedelta(seconds=ttl_seconds),
            )
            self._leases[run_id] = (lease, True)
            return lease.model_copy(deep=True)

    async def renew_execution(
        self,
        lease: FlowExecutionLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowExecutionLease:
        _validate_lease_arguments(lease.owner_id, ttl_seconds)
        heartbeat_at = now or self._clock()
        async with self._lock:
            current, active = self._require_lease(lease.run_id)
            if (
                not active
                or current.token != lease.token
                or current.is_expired(now=heartbeat_at)
            ):
                raise FlowLeaseLostError(
                    f"Flow execution lease for '{lease.run_id}' was lost"
                )
            renewed = current.model_copy(
                update={
                    "heartbeat_at": heartbeat_at,
                    "expires_at": heartbeat_at + timedelta(seconds=ttl_seconds),
                }
            )
            self._leases[lease.run_id] = (renewed, True)
            return renewed.model_copy(deep=True)

    async def release_execution(self, lease: FlowExecutionLease) -> None:
        async with self._lock:
            current, active = self._require_lease(lease.run_id)
            if current.token != lease.token:
                raise FlowLeaseLostError(
                    f"Flow execution lease for '{lease.run_id}' was lost"
                )
            if active:
                self._leases[lease.run_id] = (current, False)

    def _validate_execution_token(
        self,
        run_id: UUID,
        execution_token: UUID | None,
        *,
        now: datetime,
    ) -> None:
        stored = self._leases.get(run_id)
        if stored is None:
            if execution_token is not None:
                raise FlowLeaseLostError(
                    f"Flow Run '{run_id}' has no execution lease"
                )
            return
        lease, active = stored
        if (
            execution_token is None
            or not active
            or lease.token != execution_token
            or lease.is_expired(now=now)
        ):
            raise FlowLeaseLostError(
                f"Flow execution lease for '{run_id}' is not valid"
            )

    def _require_lease(
        self,
        run_id: UUID,
    ) -> tuple[FlowExecutionLease, bool]:
        try:
            return self._leases[run_id]
        except KeyError as exc:
            raise FlowLeaseLostError(
                f"Flow Run '{run_id}' has no execution lease"
            ) from exc

    @staticmethod
    def _materialize_events(
        run_id: UUID,
        drafts: tuple[FlowEventDraft, ...],
        *,
        start_sequence: int,
    ) -> tuple[RuntimeEvent, ...]:
        return tuple(
            RuntimeEvent(
                run_id=run_id,
                sequence=start_sequence + offset,
                type=draft.type,
                data=draft.data,
            )
            for offset, draft in enumerate(drafts)
        )


def _copy_state(state: FlowRunState) -> FlowRunState:
    return state.model_copy(deep=True)


def _copy_events(events: tuple[RuntimeEvent, ...]) -> tuple[RuntimeEvent, ...]:
    return tuple(event.model_copy(deep=True) for event in events)


def validate_flow_replacement(
    current: FlowRunState,
    replacement: FlowRunState,
    *,
    expected_revision: int,
) -> None:
    """Validate the CAS and immutable fields shared by repository adapters."""
    if current.revision != expected_revision:
        raise FlowRevisionConflictError(
            f"Flow Run '{current.run_id}' revision conflict: "
            f"expected {expected_revision}, found {current.revision}"
        )
    if replacement.run_id != current.run_id:
        raise ValueError("committed Flow Run changed its run_id")
    if replacement.revision != expected_revision + 1:
        raise ValueError(
            "committed Flow Run revision must be exactly one greater "
            "than expected_revision"
        )
    if (
        replacement.definition_id != current.definition_id
        or replacement.definition_version != current.definition_version
        or replacement.definition_fingerprint != current.definition_fingerprint
        or replacement.budget != current.budget
        or replacement.deadline_at != current.deadline_at
        or replacement.created_at != current.created_at
    ):
        raise ValueError("committed Flow Run changed immutable identity")


def _validate_lease_arguments(owner_id: str, ttl_seconds: float) -> None:
    if not owner_id.strip():
        raise ValueError("Flow lease owner_id must not be blank")
    if len(owner_id) > 256:
        raise ValueError("Flow lease owner_id must not exceed 256 characters")
    if ttl_seconds <= 0:
        raise ValueError("Flow lease ttl_seconds must be greater than zero")
