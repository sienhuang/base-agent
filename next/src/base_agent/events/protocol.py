"""Provider-neutral Event persistence and streaming contracts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol, runtime_checkable

from base_agent.models import Event


@runtime_checkable
class EventSink(Protocol):
    """Atomically assign a per-Run sequence and persist one Event."""

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Event: ...


@runtime_checkable
class EventStore(EventSink, Protocol):
    """Authoritative ordered Event history for cursor replay."""

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]: ...


@runtime_checkable
class EventSubscription(Protocol):
    """A live Event iterator with deterministic resource cleanup."""

    def __aiter__(self) -> AsyncIterator[Event]: ...

    async def __anext__(self) -> Event: ...

    async def aclose(self) -> None: ...


@runtime_checkable
class EventStream(Protocol):
    """Optional replay-then-follow capability layered over an EventStore."""

    def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> EventSubscription: ...
