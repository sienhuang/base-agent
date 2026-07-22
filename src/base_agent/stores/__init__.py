"""Persistence ports and dependency-free in-memory defaults."""

from base_agent.stores.errors import (
    ArtifactNotFoundError,
    AttachmentNotFoundError,
    CheckpointNotFoundError,
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunNotFoundError,
)
from base_agent.stores.memory import (
    InMemoryArtifactStore,
    InMemoryCheckpointStore,
    InMemoryEventStore,
    InMemoryRunStore,
)
from base_agent.stores.protocol import (
    ArtifactStore,
    CheckpointStore,
    EventSink,
    EventStore,
    EventStream,
    RunStore,
)

__all__ = [
    "ArtifactNotFoundError",
    "ArtifactStore",
    "AttachmentNotFoundError",
    "CheckpointNotFoundError",
    "CheckpointStore",
    "EventSink",
    "EventStore",
    "EventStream",
    "InMemoryArtifactStore",
    "InMemoryCheckpointStore",
    "InMemoryEventStore",
    "InMemoryRunStore",
    "RunAlreadyExistsError",
    "RunNotCancellableError",
    "RunNotFoundError",
    "RunStore",
]
