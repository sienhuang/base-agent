"""Scoped, observable wrapper around a configured Memory implementation."""

from __future__ import annotations

import asyncio
import hashlib

from dsh_base_agent.events import EventStore
from dsh_base_agent.memory.models import (
    MemoryCandidate,
    MemoryRef,
    MemoryWrite,
    RecallQuery,
)
from dsh_base_agent.memory.protocol import Memory


class MemoryRuntime:
    def __init__(
        self,
        memory: Memory | None,
        event_store: EventStore,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._memory = memory
        self._events = event_store
        self._timeout_seconds = timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._memory is not None

    @property
    def name(self) -> str:
        return self._memory.name if self._memory is not None else "disabled"

    def access(self, run_id: str) -> _ScopedMemoryAccess:
        return _ScopedMemoryAccess(self, run_id)

    async def recall(
        self,
        run_id: str,
        query: RecallQuery,
    ) -> tuple[MemoryCandidate, ...]:
        await self._events.append(
            run_id,
            "memory.recall.started",
            {
                "tenant": query.tenant,
                "namespace": query.namespace,
                "limit": query.limit,
                "memory": self._memory.name if self._memory is not None else None,
            },
        )
        if self._memory is None:
            await self._events.append(run_id, "memory.recall.completed", {"candidates": []})
            return ()
        try:
            candidates = await asyncio.wait_for(
                self._memory.recall(query),
                timeout=self._timeout_seconds,
            )
            _validate_scope(query, candidates)
        except BaseException as exc:
            await self._events.append(
                run_id,
                "memory.recall.failed",
                {"error_type": type(exc).__name__},
            )
            raise
        await self._events.append(
            run_id,
            "memory.recall.completed",
            {
                "candidates": [
                    {
                        "ref": item.ref,
                        "score": item.score,
                        "digest": hashlib.sha256(item.content.encode()).hexdigest(),
                    }
                    for item in candidates
                ]
            },
        )
        return candidates

    async def write(self, run_id: str, command: MemoryWrite) -> MemoryRef:
        if self._memory is None:
            raise RuntimeError("Memory is not configured")
        await self._events.append(
            run_id,
            "memory.write.started",
            {
                "tenant": command.tenant,
                "namespace": command.namespace,
                "source_event_ref": command.source_event_ref,
                "idempotency_key": command.idempotency_key,
            },
        )
        try:
            ref = await asyncio.wait_for(
                self._memory.write(command),
                timeout=self._timeout_seconds,
            )
            if ref.tenant != command.tenant or ref.namespace != command.namespace:
                raise RuntimeError("Memory returned a reference outside the requested scope")
        except BaseException as exc:
            await self._events.append(
                run_id,
                "memory.write.failed",
                {"error_type": type(exc).__name__},
            )
            raise
        await self._events.append(
            run_id,
            "memory.write.completed",
            {"ref": ref.ref, "tenant": ref.tenant, "namespace": ref.namespace},
        )
        return ref


class _ScopedMemoryAccess:
    def __init__(self, runtime: MemoryRuntime, run_id: str) -> None:
        self._runtime = runtime
        self._run_id = run_id
        self._recalled: dict[str, MemoryCandidate] = {}

    @property
    def recalled_refs(self) -> frozenset[str]:
        return frozenset(self._recalled)

    async def recall(self, query: RecallQuery) -> tuple[MemoryCandidate, ...]:
        candidates = await self._runtime.recall(self._run_id, query)
        self._recalled.update((item.ref, item) for item in candidates)
        return candidates

    def resolve(self, refs: tuple[str, ...]) -> tuple[MemoryCandidate, ...]:
        try:
            return tuple(self._recalled[ref] for ref in refs)
        except KeyError as exc:
            raise ValueError(f"Memory ref '{exc.args[0]}' was not recalled") from exc


def _validate_scope(
    query: RecallQuery,
    candidates: tuple[MemoryCandidate, ...],
) -> None:
    if len(candidates) > query.limit:
        raise RuntimeError("Memory returned more candidates than the requested limit")
    refs: set[str] = set()
    for candidate in candidates:
        if candidate.tenant != query.tenant or candidate.namespace != query.namespace:
            raise RuntimeError("Memory returned a candidate outside the requested scope")
        if candidate.ref in refs:
            raise RuntimeError("Memory returned duplicate candidate references")
        refs.add(candidate.ref)
