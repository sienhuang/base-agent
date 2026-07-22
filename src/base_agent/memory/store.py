"""Small deterministic lexical memory store for local development and tests."""

import asyncio
import re
from collections.abc import Iterable

from base_agent.models import MemoryMatch, MemoryQuery, MemoryRecord


class InMemoryMemoryStore:
    """Retrieve immutable records by token overlap without external services."""

    def __init__(self, records: Iterable[MemoryRecord] = ()) -> None:
        self._records = {record.id: record.model_copy(deep=True) for record in records}
        self._lock = asyncio.Lock()

    async def add(self, record: MemoryRecord) -> None:
        async with self._lock:
            self._records[record.id] = record.model_copy(deep=True)

    async def search(self, query: MemoryQuery) -> tuple[MemoryMatch, ...]:
        query_tokens = _tokens(query.text)
        async with self._lock:
            records = tuple(record.model_copy(deep=True) for record in self._records.values())
        matches = []
        for record in records:
            if query.namespace is not None and record.namespace != query.namespace:
                continue
            if any(record.metadata.get(key) != value for key, value in query.filters.items()):
                continue
            record_tokens = _tokens(record.content)
            score = len(query_tokens & record_tokens) / len(query_tokens)
            if score > 0:
                matches.append(MemoryMatch(record=record, score=score))
        matches.sort(
            key=lambda match: (
                -match.score,
                -match.record.created_at.timestamp(),
                str(match.record.id),
            )
        )
        return tuple(matches[: query.limit])


def _tokens(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"\w+", value.casefold()))
