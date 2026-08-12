"""Default no-op Memory provider."""

from __future__ import annotations

from base_agent.memory.models import MemoryObservation, MemoryRecallRequest, MemoryRecord


class DisabledMemoryProvider:
    @property
    def name(self) -> str:
        return "disabled"

    @property
    def enabled(self) -> bool:
        return False

    async def recall(self, request: MemoryRecallRequest, /) -> tuple[MemoryRecord, ...]:
        return ()


class DisabledMemoryWriter:
    @property
    def name(self) -> str:
        return "disabled"

    @property
    def enabled(self) -> bool:
        return False

    def enqueue(self, observation: MemoryObservation, /) -> None:
        return None
