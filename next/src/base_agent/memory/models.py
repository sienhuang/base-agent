"""Typed values for optional, scoped long-term Memory recall."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from base_agent.models import Message


class MemoryKind(StrEnum):
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    PROCEDURAL = "procedural"


class MemoryTrust(StrEnum):
    APPLICATION = "application"
    USER_ASSERTED = "user_asserted"
    MODEL_INFERRED = "model_inferred"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class MemoryScope:
    """Application-owned namespace; providers must not infer tenant identity."""

    namespace: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("memory scope namespace must not be empty")
        if any(not part.strip() for part in self.namespace):
            raise ValueError("memory scope namespace parts must not be blank")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One versioned Memory candidate returned by a provider."""

    id: str
    kind: MemoryKind
    content: str
    scope: MemoryScope
    trust: MemoryTrust = MemoryTrust.MODEL_INFERRED
    version: str = "1"
    relevance_score: float | None = None
    source_message_ids: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("memory id must not be blank")
        if not self.content.strip():
            raise ValueError("memory content must not be blank")
        if not self.version.strip():
            raise ValueError("memory version must not be blank")
        if self.relevance_score is not None and not 0 <= self.relevance_score <= 1:
            raise ValueError("memory relevance_score must be between 0 and 1")
        if len(set(self.source_message_ids)) != len(self.source_message_ids):
            raise ValueError("memory source_message_ids must not contain duplicates")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class MemoryRecallRequest:
    agent_id: str
    run_id: str
    query: str
    scope: MemoryScope
    limit: int

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("memory recall agent_id must not be blank")
        if not self.run_id.strip():
            raise ValueError("memory recall run_id must not be blank")
        if not self.query.strip():
            raise ValueError("memory recall query must not be blank")
        if self.limit < 1:
            raise ValueError("memory recall limit must be at least 1")


@dataclass(frozen=True, slots=True)
class MemoryObservation:
    """A successful Run transcript offered to a non-blocking Memory writer."""

    agent_id: str
    run_id: str
    scope: MemoryScope
    messages: tuple[Message, ...]
    output: str

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("memory observation agent_id must not be blank")
        if not self.run_id.strip():
            raise ValueError("memory observation run_id must not be blank")
        if not self.messages:
            raise ValueError("memory observation messages must not be empty")


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """The fixed Memory candidates reused by every model turn in one Run."""

    enabled: bool
    provider: str
    scope: MemoryScope | None
    records: tuple[MemoryRecord, ...] = ()

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("memory provider must not be blank")
        if self.enabled and self.scope is None:
            raise ValueError("enabled Memory requires an explicit scope")
        if not self.enabled and (self.scope is not None or self.records):
            raise ValueError("disabled Memory cannot contain a scope or records")
        if len({record.id for record in self.records}) != len(self.records):
            raise ValueError("memory snapshot records must have unique ids")
        if self.scope is not None and any(record.scope != self.scope for record in self.records):
            raise ValueError("memory snapshot records must match the requested scope")

    @classmethod
    def disabled(cls) -> MemorySnapshot:
        return cls(enabled=False, provider="disabled", scope=None)
