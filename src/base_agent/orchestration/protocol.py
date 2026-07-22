"""Contracts for replacing the runtime's orchestration behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from base_agent.artifacts import ArtifactManager
from base_agent.memory import MemoryManager
from base_agent.providers import ModelProvider
from base_agent.resources import ResourceManager
from base_agent.runtime.context import RuntimeContext
from base_agent.stores import EventStore, RunStore
from base_agent.supervision import Supervisor
from base_agent.tools import ToolExecutor, ToolRegistry


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    """Run-scoped dependencies made available to an orchestration strategy."""

    provider: ModelProvider
    tool_registry: ToolRegistry | None
    tool_executor: ToolExecutor | None
    run_store: RunStore
    event_store: EventStore
    supervisor: Supervisor
    resources: ResourceManager
    artifacts: ArtifactManager
    memories: MemoryManager


@runtime_checkable
class OrchestrationStrategy(Protocol):
    """Advance a running context by one bounded orchestration turn."""

    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None: ...
