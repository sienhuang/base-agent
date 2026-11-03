"""The public Memory capability port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dsh_base_agent.memory.models import (
    MemoryCandidate,
    MemoryRef,
    MemoryWrite,
    RecallQuery,
)


@runtime_checkable
class Memory(Protocol):
    @property
    def name(self) -> str: ...

    async def recall(self, query: RecallQuery) -> tuple[MemoryCandidate, ...]: ...

    async def write(self, command: MemoryWrite) -> MemoryRef: ...


@runtime_checkable
class MemoryAccess(Protocol):
    """Recall-only view made available to ContextPolicy."""

    @property
    def recalled_refs(self) -> frozenset[str]: ...

    async def recall(self, query: RecallQuery) -> tuple[MemoryCandidate, ...]: ...

    def resolve(self, refs: tuple[str, ...]) -> tuple[MemoryCandidate, ...]: ...
