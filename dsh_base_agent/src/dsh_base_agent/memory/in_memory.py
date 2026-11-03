"""Deterministic process-local Memory for tests and local development."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from dsh_base_agent.memory.models import (
    MemoryCandidate,
    MemoryRef,
    MemoryWrite,
    RecallQuery,
)


class InMemoryMemory:
    name = "in-memory"

    def __init__(self) -> None:
        self._records: dict[str, MemoryCandidate] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}
        self._lock = asyncio.Lock()

    async def write(self, command: MemoryWrite) -> MemoryRef:
        key = (command.tenant, command.namespace, command.idempotency_key)
        async with self._lock:
            existing = self._idempotency.get(key)
            if existing is not None:
                return MemoryRef(
                    ref=existing,
                    tenant=command.tenant,
                    namespace=command.namespace,
                )
            ref = f"memory-{uuid4().hex}"
            self._records[ref] = MemoryCandidate(
                ref=ref,
                tenant=command.tenant,
                namespace=command.namespace,
                content=command.content,
                score=1.0,
                source=command.source_event_ref,
                metadata={**command.metadata, "subject": command.subject},
            )
            self._idempotency[key] = ref
            return MemoryRef(
                ref=ref,
                tenant=command.tenant,
                namespace=command.namespace,
            )

    async def recall(self, query: RecallQuery) -> tuple[MemoryCandidate, ...]:
        async with self._lock:
            candidates: list[MemoryCandidate] = []
            for record in self._records.values():
                if record.tenant != query.tenant or record.namespace != query.namespace:
                    continue
                if query.subject is not None and record.metadata.get("subject") != query.subject:
                    continue
                if any(record.metadata.get(key) != value for key, value in query.filters.items()):
                    continue
                score = _text_score(query.text, record.content)
                if score <= 0:
                    continue
                candidates.append(record.model_copy(update={"score": score}))
            candidates.sort(key=lambda item: (-item.score, item.ref))
            return tuple(candidates[: query.limit])


def _text_score(query: str, content: str) -> float:
    normalized_query = query.casefold().strip()
    normalized_content = content.casefold()
    if normalized_query in normalized_content:
        return 1.0
    query_terms = set(normalized_query.split())
    if not query_terms:
        return 0.0
    content_terms = set(normalized_content.split())
    return len(query_terms & content_terms) / len(query_terms)
