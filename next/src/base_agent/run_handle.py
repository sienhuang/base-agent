"""Process-local handle over one background Agent Run and its EventStore."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from base_agent.events import EventStore, EventStream
from base_agent.models import Event, RunResult, RunStatus

_RunOperation = Callable[[], Coroutine[Any, Any, RunResult]]


class EventStreamingNotSupportedError(RuntimeError):
    """The configured EventStore does not expose live subscriptions."""


class RunHandle:
    """Observe and await one process-local background Run."""

    __slots__ = ("_event_store", "_task", "run_id")

    def __init__(
        self,
        *,
        run_id: str,
        task: asyncio.Task[RunResult],
        event_store: EventStore,
    ) -> None:
        self.run_id = run_id
        self._task = task
        self._event_store = event_store

    @classmethod
    def _launch(
        cls,
        *,
        run_id: str,
        operation: _RunOperation,
        event_store: EventStore,
    ) -> RunHandle:
        task = asyncio.create_task(operation(), name=f"base-agent-run-{run_id}")
        return cls(run_id=run_id, task=task, event_store=event_store)

    @property
    def status(self) -> RunStatus:
        """Return RUNNING until the background task reaches a terminal result."""
        if not self._task.done():
            return RunStatus.RUNNING
        if self._task.cancelled():
            return RunStatus.FAILED
        try:
            return self._task.result().status
        except Exception:
            return RunStatus.FAILED

    async def wait(self) -> RunResult:
        """Wait for completion without letting waiter cancellation stop the Run."""
        return await asyncio.shield(self._task)

    async def events(self, *, after_sequence: int = 0) -> tuple[Event, ...]:
        """Read persisted Events strictly after the cursor."""
        _validate_cursor(after_sequence)
        return await self._event_store.list(
            self.run_id,
            after_sequence=after_sequence,
        )

    def stream(self, *, after_sequence: int = 0) -> AsyncIterator[Event]:
        """Replay Events after a cursor, then follow new Events until completion."""
        _validate_cursor(after_sequence)
        if not isinstance(self._event_store, EventStream):
            raise EventStreamingNotSupportedError(
                "the configured EventStore does not support live subscriptions"
            )
        return self._follow_event_stream(
            self._event_store,
            after_sequence=after_sequence,
        )

    async def _follow_event_stream(
        self,
        event_stream: EventStream,
        *,
        after_sequence: int,
    ) -> AsyncIterator[Event]:
        """Stop a local subscription if the execution Task exits without a terminal Event."""
        cursor = after_sequence
        subscription = event_stream.subscribe(
            self.run_id,
            after_sequence=after_sequence,
        )
        pending_event: asyncio.Task[Event] | None = None

        async def next_event() -> Event:
            return await anext(subscription)

        try:
            while True:
                pending_event = asyncio.create_task(next_event())
                completed, _ = await asyncio.wait(
                    (pending_event, self._task),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending_event in completed:
                    try:
                        event = pending_event.result()
                    except StopAsyncIteration:
                        return
                    pending_event = None
                    cursor = event.sequence
                    yield event
                    continue

                pending_event.cancel()
                try:
                    await pending_event
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
                pending_event = None
                for event in await self._event_store.list(
                    self.run_id,
                    after_sequence=cursor,
                ):
                    cursor = event.sequence
                    yield event
                return
        finally:
            if pending_event is not None:
                pending_event.cancel()
                try:
                    await pending_event
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass
            await subscription.aclose()


def _validate_cursor(after_sequence: int) -> None:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
