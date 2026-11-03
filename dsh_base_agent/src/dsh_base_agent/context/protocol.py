"""The sole dynamic-context policy extension point."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from dsh_base_agent.context.models import ContextDecision, ContextPolicyContext


@runtime_checkable
class ContextPolicy(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    async def decide(self, context: ContextPolicyContext) -> ContextDecision: ...
