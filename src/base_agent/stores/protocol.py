"""Persistence protocols consumed by the core runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from base_agent.models import Artifact, Attachment, EventType, Run, RuntimeEvent

if TYPE_CHECKING:
    from base_agent.runtime.checkpoint import RuntimeCheckpoint


@runtime_checkable
class RunStore(Protocol):
    async def create(self, run: Run) -> None: ...

    async def get(self, run_id: UUID) -> Run: ...

    async def save(self, run: Run) -> None: ...

    async def request_cancel(self, run_id: UUID) -> Run: ...

    async def is_cancel_requested(self, run_id: UUID) -> bool: ...


@runtime_checkable
class EventSink(Protocol):
    async def emit(
        self,
        run_id: UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> RuntimeEvent: ...


@runtime_checkable
class EventStore(EventSink, Protocol):
    async def list(self, run_id: UUID) -> tuple[RuntimeEvent, ...]: ...


@runtime_checkable
class EventStream(Protocol):
    """Optional live, cursor-based event capability implemented by streaming stores."""

    def subscribe(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[RuntimeEvent]: ...


@runtime_checkable
class CheckpointStore(Protocol):
    """Persistence boundary for suspended Runtime state."""

    async def save(self, checkpoint: RuntimeCheckpoint) -> None: ...

    async def load(self, run_id: UUID) -> RuntimeCheckpoint: ...

    async def claim(self, run_id: UUID) -> RuntimeCheckpoint: ...

    async def delete(self, run_id: UUID) -> None: ...


@runtime_checkable
class ArtifactStore(Protocol):
    """Binary content boundary; events and checkpoints retain references only."""

    async def add_attachment(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> Attachment: ...

    async def get_attachment(self, attachment_id: UUID) -> Attachment: ...

    async def create_artifact(
        self,
        run_id: UUID,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact: ...

    async def get_artifact(self, artifact_id: UUID) -> Artifact: ...

    async def read(self, content_id: UUID) -> bytes: ...

    async def list_artifacts(self, run_id: UUID) -> tuple[Artifact, ...]: ...
