"""Application-facing Context bundles and selection catalog."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import MappingProxyType

from dsh_base_agent.context.builtin import KeepAllContextPolicy
from dsh_base_agent.context.models import ContextBlock
from dsh_base_agent.context.protocol import ContextPolicy
from dsh_base_agent.memory import MemoryScope

_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class Context:
    """One named business Context configuration selected by an Agent.

    A Context groups the policy, candidate blocks, Memory scope and token
    budget that must change together.  The policy still owns the final choice
    of what enters a DSH request.
    """

    name: str
    policy: ContextPolicy = field(default_factory=KeepAllContextPolicy)
    blocks: tuple[ContextBlock, ...] = ()
    memory_scope: MemoryScope | None = None
    memory_writeback: bool = False
    available_tokens: int = 32_000

    def __post_init__(self) -> None:
        if not self.name or not _NAME_PATTERN.fullmatch(self.name):
            raise ValueError(
                "Context name must contain only letters, numbers, '.', '_' or '-'"
            )
        if self.available_tokens < 1:
            raise ValueError("Context available_tokens must be positive")
        if not self.policy.name.strip() or not self.policy.version.strip():
            raise ValueError("Context policy name and version must not be blank")
        ids = [block.id for block in self.blocks]
        if len(set(ids)) != len(ids):
            raise ValueError(f"Context '{self.name}' contains duplicate block ids")


class ContextCatalog:
    """Immutable lookup of application-owned Context definitions."""

    def __init__(self, contexts: Iterable[Context] = ()) -> None:
        registered = tuple(contexts)
        by_name = {context.name: context for context in registered}
        if len(by_name) != len(registered):
            raise ValueError("registered Contexts must have unique names")
        self._contexts = registered
        self._by_name = MappingProxyType(by_name)

    @classmethod
    def of(cls, *contexts: Context) -> ContextCatalog:
        return cls(contexts)

    @property
    def contexts(self) -> tuple[Context, ...]:
        return self._contexts

    def resolve(self, name: str) -> Context:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise ValueError(f"Context '{name}' is not registered") from exc


__all__ = ["Context", "ContextCatalog"]
