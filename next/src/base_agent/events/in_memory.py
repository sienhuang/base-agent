"""Dependency-free process-local EventStore and EventStream."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncGenerator, Mapping
from typing import Any

from base_agent.models import Event

_TERMINAL_EVENT_KINDS = frozenset(
    {
        "flow.succeeded",
        "flow.failed",
        "flow.cancelled",
        "run.succeeded",
        "run.failed",
        "run.cancelled",
    }
)


class InMemoryEventStore:
    """Keep ordered per-Run history and notify process-local subscribers."""

    def __init__(self) -> None:
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event:
        async with self._changed:
            events = self._events[run_id]
            event = Event(
                sequence=len(events) + 1,
                kind=kind,
                run_id=run_id,
                payload=payload or {},
                turn=turn,
            )
            events.append(event)
            self._changed.notify_all()
            return event

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]:
        _validate_cursor(after_sequence)
        async with self._lock:
            return tuple(
                event
                for event in self._events[run_id]
                if event.sequence > after_sequence
            )

    async def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncGenerator[Event, None]:
        _validate_cursor(after_sequence)
        cursor = after_sequence
        while True:
            async with self._changed:
                while True:
                    events = tuple(
                        event
                        for event in self._events[run_id]
                        if event.sequence > cursor
                    )
                    terminal = _has_terminal_event(self._events[run_id])
                    if events or terminal:
                        break
                    await self._changed.wait()

            for event in events:
                cursor = event.sequence
                yield event
                if event.kind in _TERMINAL_EVENT_KINDS:
                    return
            if terminal:
                return


def _has_terminal_event(events: list[Event]) -> bool:
    return bool(events) and events[-1].kind in _TERMINAL_EVENT_KINDS


def _validate_cursor(after_sequence: int) -> None:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")
