"""Shared Event publication with fail-open Observer fan-out."""

from __future__ import annotations

import inspect
from collections.abc import Sequence
from typing import Any

from base_agent.events.protocol import EventStore
from base_agent.models import Event
from base_agent.observability import Observer


class EventEmitter:
    """Commit Events to one authoritative store before notifying Observers."""

    def __init__(
        self,
        run_id: str,
        event_store: EventStore,
        observers: Sequence[Observer] = (),
    ) -> None:
        self._run_id = run_id
        self._event_store = event_store
        self._observers = tuple(observers)

    @property
    def run_id(self) -> str:
        return self._run_id

    async def events(self) -> tuple[Event, ...]:
        return await self._event_store.list(self._run_id)

    async def emit(
        self,
        kind: str,
        *,
        turn: int | None = None,
        **payload: Any,
    ) -> Event:
        event = await self._event_store.emit(
            self._run_id,
            kind,
            payload,
            turn=turn,
        )
        for observer in self._observers:
            try:
                value = observer(event)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                # Observers report facts and cannot change execution.
                continue
        return event
