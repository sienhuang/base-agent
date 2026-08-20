"""Process-local handle over one background Flow execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any, Protocol

from base_agent.events import EventStore, EventStream
from base_agent.flows.models import FlowStatus
from base_agent.models import Event
from base_agent.run_handle import EventStreamingNotSupportedError


class _FlowResult(Protocol):
    @property
    def status(self) -> FlowStatus: ...


type _FlowOperation[FlowResultT: _FlowResult] = Callable[
    [],
    Coroutine[Any, Any, FlowResultT],
]


class FlowRunNotCancellableError(RuntimeError):
    """The process-local Flow already reached a non-cancelled terminal state."""


class FlowRunHandle[FlowResultT: _FlowResult]:
    """Observe, await, and cancel one process-local background Flow."""

    __slots__ = (
        "_cancel_lock",
        "_cancel_operation",
        "_cancel_requested",
        "_event_store",
        "_started",
        "_settled_result",
        "_task",
        "flow_run_id",
    )

    def __init__(
        self,
        *,
        flow_run_id: str,
        task: asyncio.Task[FlowResultT],
        event_store: EventStore,
        started: asyncio.Event,
        cancel_operation: _FlowOperation[FlowResultT],
    ) -> None:
        self.flow_run_id = flow_run_id
        self._task = task
        self._event_store = event_store
        self._started = started
        self._cancel_operation = cancel_operation
        self._settled_result: FlowResultT | None = None
        self._cancel_lock = asyncio.Lock()
        self._cancel_requested = False

    @classmethod
    def _launch(
        cls,
        *,
        flow_run_id: str,
        operation: _FlowOperation[FlowResultT],
        cancel_operation: _FlowOperation[FlowResultT],
        event_store: EventStore,
    ) -> FlowRunHandle[FlowResultT]:
        started = asyncio.Event()

        async def run_operation() -> FlowResultT:
            started.set()
            try:
                return await operation()
            except asyncio.CancelledError:
                return await cancel_operation()

        task = asyncio.create_task(
            run_operation(),
            name=f"base-agent-flow-{flow_run_id}",
        )
        return cls(
            flow_run_id=flow_run_id,
            task=task,
            event_store=event_store,
            started=started,
            cancel_operation=cancel_operation,
        )

    @property
    def status(self) -> FlowStatus:
        if self._settled_result is not None:
            return self._settled_result.status
        if not self._task.done():
            return FlowStatus.RUNNING
        if self._task.cancelled():
            return FlowStatus.CANCELLED
        try:
            return self._task.result().status
        except Exception:
            return FlowStatus.FAILED

    async def wait(self) -> FlowResultT:
        if self._settled_result is not None:
            return self._settled_result
        return await asyncio.shield(self._task)

    async def cancel(self) -> FlowResultT:
        async with self._cancel_lock:
            if self._cancel_requested:
                if self._settled_result is not None:
                    return self._settled_result
                return await asyncio.shield(self._task)

            await self._started.wait()
            if self._task.done():
                result = self._task.result()
                if result.status is FlowStatus.WAITING:
                    self._cancel_requested = True
                    self._settled_result = await self._cancel_operation()
                    return self._settled_result
                return self._cancelled_or_raise()

            terminal = _terminal_status(await self._event_store.list(self.flow_run_id))
            if terminal is not None:
                raise FlowRunNotCancellableError(
                    f"Flow '{self.flow_run_id}' already committed status "
                    f"'{terminal.value}' and cannot be cancelled"
                )

            self._cancel_requested = True
            self._task.cancel("flow cancellation requested")
            result = await asyncio.shield(self._task)
            if result.status is not FlowStatus.CANCELLED:
                raise FlowRunNotCancellableError(
                    f"Flow '{self.flow_run_id}' reached status '{result.status.value}' "
                    "before cancellation"
                )
            return result

    async def events(self, *, after_sequence: int = 0) -> tuple[Event, ...]:
        _validate_cursor(after_sequence)
        return await self._event_store.list(
            self.flow_run_id,
            after_sequence=after_sequence,
        )

    def stream(self, *, after_sequence: int = 0) -> AsyncIterator[Event]:
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
        cursor = after_sequence
        subscription = event_stream.subscribe(
            self.flow_run_id,
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
                    self.flow_run_id,
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

    def _cancelled_or_raise(self) -> FlowResultT:
        if self._task.cancelled():
            raise FlowRunNotCancellableError(
                f"Flow '{self.flow_run_id}' exited without a cancellable result"
            )
        try:
            result = self._task.result()
        except Exception as exc:
            raise FlowRunNotCancellableError(
                f"Flow '{self.flow_run_id}' failed and cannot be cancelled"
            ) from exc
        if result.status is FlowStatus.CANCELLED:
            self._cancel_requested = True
            return result
        raise FlowRunNotCancellableError(
            f"Flow '{self.flow_run_id}' already reached status '{result.status.value}' "
            "and cannot be cancelled"
        )


def _validate_cursor(after_sequence: int) -> None:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")


def _terminal_status(events: tuple[Event, ...]) -> FlowStatus | None:
    if not events:
        return None
    return {
        "flow.succeeded": FlowStatus.SUCCEEDED,
        "flow.failed": FlowStatus.FAILED,
        "flow.cancelled": FlowStatus.CANCELLED,
    }.get(events[-1].kind)
