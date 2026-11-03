"""Durable event storage owned by dsh_base_agent."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections import defaultdict
from collections.abc import AsyncGenerator, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from dsh_base_agent.models import Event

_TERMINAL_KINDS = frozenset(
    {
        "run.succeeded",
        "run.failed",
        "run.cancelled",
        "run.waiting",
        "run.blocked",
        "run.interrupted",
        "run.recovery_required",
        "flow.succeeded",
        "flow.failed",
        "flow.cancelled",
        "flow.waiting",
        "flow.blocked",
        "flow.recovery_required",
    }
)


class EventStoreError(RuntimeError):
    """A durable event operation failed."""


@runtime_checkable
class EventStore(Protocol):
    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event: ...

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]: ...

    async def append(
        self,
        stream_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Event: ...

    def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncGenerator[Event, None]: ...


class JsonlEventStore:
    """Single-process JSONL fact store with cursor replay and live subscriptions.

    Agent, DSH adapter, Flow and HTTP server all use this one stream.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._changed = asyncio.Condition()

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event:
        _validate_identifier(run_id, "run_id")
        _validate_identifier(kind, "kind")
        normalized = dict(payload or {})
        try:
            json.dumps(normalized, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("event payload must be finite JSON") from exc

        async with self._locks[run_id]:
            try:
                event = await asyncio.to_thread(
                    self._emit_sync,
                    run_id,
                    kind,
                    normalized,
                    turn,
                )
            except OSError as exc:
                raise EventStoreError(f"failed to append '{kind}' to Run '{run_id}'") from exc
        async with self._changed:
            self._changed.notify_all()
        return event

    async def append(
        self,
        stream_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        """Compatibility alias used by Context and Memory runtimes."""

        return await self.emit(stream_id, kind, payload)

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]:
        _validate_identifier(run_id, "run_id")
        _validate_cursor(after_sequence)
        async with self._locks[run_id]:
            return await asyncio.to_thread(self._list_sync, run_id, after_sequence)

    async def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncGenerator[Event, None]:
        """Replay after a cursor and then follow until a terminal fact."""

        _validate_identifier(run_id, "run_id")
        _validate_cursor(after_sequence)
        cursor = after_sequence
        while True:
            # Observe the stream and enter wait under one notification lock so
            # an append cannot land in the list-then-wait race window.
            async with self._changed:
                events = await self.list(run_id, after_sequence=cursor)
                if not events:
                    current = await self.list(run_id)
                    if current and current[-1].kind in _TERMINAL_KINDS:
                        return
                    await self._changed.wait()
                    continue
            for event in events:
                cursor = event.sequence
                yield event
                if event.kind in _TERMINAL_KINDS:
                    return

    def _emit_sync(
        self,
        run_id: str,
        kind: str,
        payload: dict[str, Any],
        turn: int | None,
    ) -> Event:
        path = self._path(run_id)
        previous = self._list_sync(run_id, 0)
        event = Event(
            sequence=previous[-1].sequence + 1 if previous else 1,
            kind=kind,
            run_id=run_id,
            payload=payload,
            turn=turn,
        )
        record = {
            "sequence": event.sequence,
            "kind": event.kind,
            "run_id": event.run_id,
            "payload": dict(event.payload),
            "turn": event.turn,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        return event

    def _list_sync(self, run_id: str, after_sequence: int) -> tuple[Event, ...]:
        path = self._path(run_id)
        if not path.exists():
            return ()
        events: list[Event] = []
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    event = Event(
                        sequence=value["sequence"],
                        kind=value["kind"],
                        run_id=value["run_id"],
                        payload=value.get("payload") or {},
                        turn=value.get("turn"),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise EventStoreError(f"invalid event at {path}:{line_number}") from exc
                if event.run_id != run_id:
                    raise EventStoreError(f"Run identity mismatch at {path}:{line_number}")
                if event.sequence != len(events) + 1:
                    raise EventStoreError(f"non-contiguous sequence at {path}:{line_number}")
                events.append(event)
        return tuple(event for event in events if event.sequence > after_sequence)

    def _path(self, run_id: str) -> Path:
        return self.root / f"{hashlib.sha256(run_id.encode()).hexdigest()}.jsonl"


class InMemoryEventStore:
    """Small standalone EventStore useful for tests and embedded Flows."""

    def __init__(self) -> None:
        self._events: defaultdict[str, list[Event]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition()

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event:
        async with self._lock:
            stream = self._events[run_id]
            event = Event(len(stream) + 1, kind, run_id, payload or {}, turn)
            stream.append(event)
        async with self._changed:
            self._changed.notify_all()
        return event

    async def append(
        self,
        stream_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
    ) -> Event:
        return await self.emit(stream_id, kind, payload)

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]:
        return tuple(
            event for event in self._events.get(run_id, ()) if event.sequence > after_sequence
        )

    async def subscribe(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> AsyncGenerator[Event, None]:
        cursor = after_sequence
        while True:
            async with self._changed:
                events = await self.list(run_id, after_sequence=cursor)
                if not events:
                    current = await self.list(run_id)
                    if current and current[-1].kind in _TERMINAL_KINDS:
                        return
                    await self._changed.wait()
                    continue
            for event in events:
                cursor = event.sequence
                yield event
                if event.kind in _TERMINAL_KINDS:
                    return


def _validate_identifier(value: str, name: str) -> None:
    if not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be non-blank without surrounding whitespace")


def _validate_cursor(after_sequence: int) -> None:
    if after_sequence < 0:
        raise ValueError("after_sequence must be non-negative")


__all__ = ["EventStore", "EventStoreError", "InMemoryEventStore", "JsonlEventStore"]
