"""Persistence-neutral memory retrieval contract."""

from typing import Protocol, runtime_checkable

from base_agent.models import MemoryMatch, MemoryQuery


@runtime_checkable
class MemoryRetriever(Protocol):
    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]: ...
