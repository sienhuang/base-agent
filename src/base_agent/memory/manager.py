"""Run-scoped memory retrieval and privacy-safe lifecycle events."""

from __future__ import annotations

from typing import TYPE_CHECKING

from base_agent.memory.protocol import MemoryRetriever
from base_agent.models import EventType, MemoryFailureMode, MemoryMatch, MemoryQuery
from base_agent.stores import EventStore

if TYPE_CHECKING:
    from base_agent.runtime.context import RuntimeContext


class MemoryNotConfiguredError(RuntimeError):
    """A consumer requested memory when no retriever was configured."""


class MemoryManager:
    """Retrieve memories without copying their content into events or Run metadata."""

    def __init__(
        self,
        *,
        context: RuntimeContext,
        retriever: MemoryRetriever | None,
        event_store: EventStore,
        limit: int = 5,
        namespace: str | None = None,
        failure_mode: MemoryFailureMode = MemoryFailureMode.BEST_EFFORT,
    ) -> None:
        if limit < 1 or limit > 100:
            raise ValueError("memory limit must be between 1 and 100")
        if namespace is not None and not namespace.strip():
            raise ValueError("memory namespace must not be blank")
        self._context = context
        self._retriever = retriever
        self._event_store = event_store
        self._limit = limit
        self._namespace = namespace
        self._failure_mode = failure_mode

    @property
    def matches(self) -> tuple[MemoryMatch, ...]:
        return self._context.memories

    async def initialize(self) -> tuple[MemoryMatch, ...]:
        if self._context.memory_initialized:
            return self._context.memories
        self._context.memory_initialized = True
        if self._retriever is None:
            return ()
        query = MemoryQuery(
            text=self._context.input_text,
            limit=self._limit,
            namespace=self._namespace,
            profile_id=self._context.profile.id,
            run_id=self._context.run_id,
        )
        try:
            matches = await self.search(query)
        except Exception as exc:
            self._context.memory_error = str(exc)
            if self._failure_mode is MemoryFailureMode.REQUIRED:
                raise
            return ()
        self._context.memories = matches
        self._context.memory_error = None
        return matches

    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        if self._retriever is None:
            raise MemoryNotConfiguredError("no memory retriever is configured")
        try:
            matches = tuple(await self._retriever.search(query))[: query.limit]
        except Exception as exc:
            await self._event_store.emit(
                self._context.run_id,
                EventType.MEMORY_FAILED,
                {"error": str(exc)},
            )
            raise
        await self._event_store.emit(
            self._context.run_id,
            EventType.MEMORY_RETRIEVED,
            {
                "limit": query.limit,
                "namespace": query.namespace,
                "matches": [
                    {"id": str(match.record.id), "score": match.score}
                    for match in matches
                ],
            },
        )
        return matches
