"""Context selection extension contract."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from base_agent.context.models import ContextInput, ContextView


@runtime_checkable
class ContextPolicy(Protocol):
    """Synchronously select a request-only view without mutating its input."""

    @property
    def name(self) -> str: ...

    def select(self, context: ContextInput, /) -> ContextView: ...

