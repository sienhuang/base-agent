"""Concurrency-safe in-memory stores used by local agents and tests."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any
from uuid import UUID

from base_agent.models import EventType, Run, RunStatus, RuntimeEvent
from base_agent.models.run import utc_now
from base_agent.stores.errors import (
    CheckpointNotFoundError,
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunNotFoundError,
)

if TYPE_CHECKING:
    from base_agent.runtime.checkpoint import RuntimeCheckpoint


class InMemoryCheckpointStore:
    """Atomic in-memory checkpoint claims for local execution and tests."""

    def __init__(self) -> None:
        self._checkpoints: dict[UUID, RuntimeCheckpoint] = {}
        self._lock = asyncio.Lock()

    async def save(self, checkpoint: RuntimeCheckpoint) -> None:
        async with self._lock:
            self._checkpoints[checkpoint.run_id] = checkpoint.model_copy(deep=True)

    async def load(self, run_id: UUID) -> RuntimeCheckpoint:
        async with self._lock:
            try:
                checkpoint = self._checkpoints[run_id]
            except KeyError as exc:
                raise CheckpointNotFoundError(
                    f"checkpoint for run '{run_id}' was not found"
                ) from exc
            return checkpoint.model_copy(deep=True)

    async def claim(self, run_id: UUID) -> RuntimeCheckpoint:
        async with self._lock:
            try:
                checkpoint = self._checkpoints.pop(run_id)
            except KeyError as exc:
                raise CheckpointNotFoundError(
                    f"checkpoint for run '{run_id}' was not found or was already claimed"
                ) from exc
            return checkpoint.model_copy(deep=True)

    async def delete(self, run_id: UUID) -> None:
        async with self._lock:
            self._checkpoints.pop(run_id, None)


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[UUID, Run] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: Run) -> None:
        async with self._lock:
            if run.id in self._runs:
                raise RunAlreadyExistsError(f"run '{run.id}' already exists")
            self._runs[run.id] = run.model_copy(deep=True)

    async def get(self, run_id: UUID) -> Run:
        async with self._lock:
            try:
                run = self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(f"run '{run_id}' was not found") from exc
            return run.model_copy(deep=True)

    async def save(self, run: Run) -> None:
        async with self._lock:
            if run.id not in self._runs:
                raise RunNotFoundError(f"run '{run.id}' was not found")
            self._runs[run.id] = run.model_copy(deep=True)

    async def request_cancel(self, run_id: UUID) -> Run:
        async with self._lock:
            try:
                run = self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(f"run '{run_id}' was not found") from exc
            if run.status not in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING}:
                raise RunNotCancellableError(
                    f"run '{run_id}' in state '{run.status.value}' cannot be cancelled"
                )
            updated = run.model_copy(
                update={"cancel_requested": True, "updated_at": utc_now()},
                deep=True,
            )
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        return (await self.get(run_id)).cancel_requested


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[UUID, list[RuntimeEvent]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)

    async def emit(
        self,
        run_id: UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        async with self._changed:
            events = self._events[run_id]
            event = RuntimeEvent(
                run_id=run_id,
                sequence=len(events) + 1,
                type=event_type,
                data=data or {},
            )
            events.append(event)
            self._changed.notify_all()
            return event.model_copy(deep=True)

    async def list(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        async with self._lock:
            return tuple(event.model_copy(deep=True) for event in self._events[run_id])

    async def subscribe(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[RuntimeEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        next_sequence = after_sequence + 1
        terminal_types = {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
            EventType.RUN_LIMIT_REACHED,
            EventType.RUN_WAITING,
        }
        while True:
            async with self._changed:
                while len(self._events[run_id]) < next_sequence and not any(
                    event.sequence >= next_sequence and event.type in terminal_types
                    for event in self._events[run_id]
                ):
                    await self._changed.wait()
                available = self._events[run_id][next_sequence - 1 :]
                batch = tuple(event.model_copy(deep=True) for event in available)
            if not batch:
                return
            for event in batch:
                yield event
                next_sequence = event.sequence + 1
                if event.type in terminal_types:
                    return
