"""Process-local lifecycle handle for a background Agent Run."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from dsh_base_agent.events import EventStore
from dsh_base_agent.models import Event, RunResult, RunStatus

type _Operation = Callable[[], Coroutine[Any, Any, RunResult]]


class RunNotCancellableError(RuntimeError):
    pass


class RunHandle:
    def __init__(
        self,
        *,
        run_id: str,
        task: asyncio.Task[RunResult],
        event_store: EventStore,
        cancel_operation: _Operation,
    ) -> None:
        self.run_id = run_id
        self._task = task
        self._event_store = event_store
        self._cancel_operation = cancel_operation
        self._cancel_lock = asyncio.Lock()
        self._settled: RunResult | None = None

    @classmethod
    def _launch(
        cls,
        *,
        run_id: str,
        operation: _Operation,
        cancel_operation: _Operation,
        event_store: EventStore,
    ) -> RunHandle:
        async def execute() -> RunResult:
            try:
                return await operation()
            except asyncio.CancelledError:
                return await cancel_operation()

        return cls(
            run_id=run_id,
            task=asyncio.create_task(execute(), name=f"dsh-agent-run-{run_id}"),
            event_store=event_store,
            cancel_operation=cancel_operation,
        )

    @property
    def status(self) -> RunStatus:
        if self._settled is not None:
            return self._settled.status
        if not self._task.done():
            return RunStatus.RUNNING
        if self._task.cancelled():
            return RunStatus.CANCELLED
        try:
            return self._task.result().status
        except Exception:
            return RunStatus.FAILED

    async def wait(self) -> RunResult:
        if self._settled is None:
            self._settled = await asyncio.shield(self._task)
        return self._settled

    async def cancel(self) -> RunResult:
        async with self._cancel_lock:
            if self._settled is not None:
                if self._settled.status is RunStatus.CANCELLED:
                    return self._settled
                raise RunNotCancellableError(
                    f"Run '{self.run_id}' already reached '{self._settled.status.value}'"
                )
            if self._task.done():
                return await self._completed_or_raise()
            self._task.cancel("run cancellation requested")
            self._settled = await asyncio.shield(self._task)
            return self._settled

    async def events(self, *, after_sequence: int = 0) -> tuple[Event, ...]:
        return await self._event_store.list(self.run_id, after_sequence=after_sequence)

    def stream(self, *, after_sequence: int = 0) -> AsyncIterator[Event]:
        return self._event_store.subscribe(
            self.run_id,
            after_sequence=after_sequence,
        )

    async def _completed_or_raise(self) -> RunResult:
        result = await asyncio.shield(self._task)
        self._settled = result
        if result.status is RunStatus.CANCELLED:
            return result
        raise RunNotCancellableError(f"Run '{self.run_id}' already reached '{result.status.value}'")


__all__ = ["RunHandle", "RunNotCancellableError"]
