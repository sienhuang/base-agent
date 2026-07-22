"""Run-scoped access to attachment content and generated artifacts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from base_agent.models import Artifact, Attachment, EventType
from base_agent.stores import ArtifactStore, EventStore

if TYPE_CHECKING:
    from base_agent.runtime.context import RuntimeContext


class ArtifactAccessError(PermissionError):
    """A Run attempted to read content outside its declared scope."""


class ArtifactManager:
    """Create outputs and read only content attached to the current Run."""

    def __init__(
        self,
        *,
        context: RuntimeContext,
        store: ArtifactStore,
        event_store: EventStore,
    ) -> None:
        self._context = context
        self._store = store
        self._event_store = event_store

    @property
    def attachments(self) -> tuple[Attachment, ...]:
        return self._context.attachments

    @property
    def artifacts(self) -> tuple[Artifact, ...]:
        return tuple(self._context.artifacts)

    async def create(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        artifact = await self._store.create_artifact(
            self._context.run_id,
            name=name,
            media_type=media_type,
            content=content,
            metadata=metadata,
        )
        self._context.artifacts.append(artifact)
        await self._event_store.emit(
            self._context.run_id,
            EventType.ARTIFACT_CREATED,
            {"artifact": artifact.model_dump(mode="json")},
        )
        return artifact

    async def read_attachment(self, attachment_id: UUID) -> bytes:
        if attachment_id not in {item.id for item in self._context.attachments}:
            raise ArtifactAccessError(
                f"attachment '{attachment_id}' is not available to this Run"
            )
        return await self._store.read(attachment_id)

    async def read_artifact(self, artifact_id: UUID) -> bytes:
        artifact = await self._store.get_artifact(artifact_id)
        if artifact.run_id != self._context.run_id:
            raise ArtifactAccessError(
                f"artifact '{artifact_id}' belongs to another Run"
            )
        return await self._store.read(artifact_id)
