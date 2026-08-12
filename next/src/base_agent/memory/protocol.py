"""Optional long-term Memory recall extension boundary."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from base_agent.memory.models import MemoryObservation, MemoryRecallRequest, MemoryRecord


@runtime_checkable
class MemoryProvider(Protocol):
    """Recall scoped candidates; writing is kept behind MemoryWriter."""

    @property
    def name(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    async def recall(self, request: MemoryRecallRequest, /) -> tuple[MemoryRecord, ...]: ...


@runtime_checkable
class MemoryWriter(Protocol):
    """Synchronously enqueue an observation; implementations perform I/O elsewhere."""

    @property
    def name(self) -> str: ...

    @property
    def enabled(self) -> bool: ...

    def enqueue(self, observation: MemoryObservation, /) -> None: ...
