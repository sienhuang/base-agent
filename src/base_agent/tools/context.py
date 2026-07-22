"""Runtime-only context injected into resource-aware Tools."""

from dataclasses import dataclass
from uuid import UUID

from base_agent.artifacts import ArtifactManager
from base_agent.memory import MemoryManager
from base_agent.resources import ResourceManager


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Capabilities available to a Tool but intentionally hidden from model arguments."""

    run_id: UUID
    resources: ResourceManager
    artifacts: ArtifactManager
    memories: MemoryManager
