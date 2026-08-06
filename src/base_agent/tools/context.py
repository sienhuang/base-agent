"""Runtime-only context injected into resource-aware Tools."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from base_agent.artifacts import ArtifactManager
from base_agent.memory import MemoryManager
from base_agent.resources import ResourceManager

if TYPE_CHECKING:
    from base_agent.models import ToolConfirmation


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Capabilities available to a Tool but intentionally hidden from model arguments."""

    run_id: UUID
    resources: ResourceManager
    artifacts: ArtifactManager
    memories: MemoryManager
    flow_run_id: UUID | None = None
    invocation_id: UUID | None = None
    tool_call_id: str | None = None
    idempotency_key: str | None = None
    confirmation: ToolConfirmation | None = None
