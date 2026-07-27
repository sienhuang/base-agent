"""Concurrency-safe in-memory stores used by local agents and tests."""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any
from uuid import UUID

from base_agent.models import (
    Artifact,
    Attachment,
    Conversation,
    ConversationTurn,
    EventType,
    Message,
    Run,
    RunStatus,
    RuntimeEvent,
)
from base_agent.models.run import utc_now
from base_agent.stores.errors import (
    ArtifactNotFoundError,
    AttachmentNotFoundError,
    CheckpointNotFoundError,
    ConversationAlreadyExistsError,
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationProfileMismatchError,
    ConversationTurnNotFoundError,
    RunAlreadyExistsError,
    RunNotCancellableError,
    RunNotFoundError,
)


class InMemoryConversationStore:
    """Concurrency-safe Conversation history for local Agents and tests."""

    def __init__(self) -> None:
        self._conversations: dict[UUID, Conversation] = {}
        self._turns: dict[UUID, list[ConversationTurn]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def create_conversation(self, conversation: Conversation) -> None:
        async with self._lock:
            if conversation.id in self._conversations:
                raise ConversationAlreadyExistsError(
                    f"conversation '{conversation.id}' already exists"
                )
            self._conversations[conversation.id] = conversation.model_copy(deep=True)

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        async with self._lock:
            return self._get_locked(conversation_id).model_copy(deep=True)

    async def begin_turn(
        self,
        conversation_id: UUID,
        *,
        run_id: UUID,
        profile_id: str,
        user_message: str,
    ) -> tuple[ConversationTurn, tuple[Message, ...]]:
        if not user_message.strip():
            raise ValueError("user_message must not be empty")
        async with self._lock:
            conversation = self._get_locked(conversation_id)
            if conversation.profile_id != profile_id:
                raise ConversationProfileMismatchError(
                    f"conversation '{conversation_id}' belongs to profile "
                    f"'{conversation.profile_id}', not '{profile_id}'"
                )
            if conversation.active_run_id is not None:
                raise ConversationBusyError(
                    f"conversation '{conversation_id}' already has active run "
                    f"'{conversation.active_run_id}'"
                )
            history = self._messages_locked(conversation_id)
            turn = ConversationTurn(
                conversation_id=conversation_id,
                sequence=conversation.version + 1,
                run_id=run_id,
                user_message=user_message,
            )
            self._turns[conversation_id].append(turn)
            self._conversations[conversation_id] = conversation.model_copy(
                update={
                    "version": turn.sequence,
                    "active_run_id": run_id,
                    "updated_at": utc_now(),
                },
                deep=True,
            )
            return turn.model_copy(deep=True), tuple(
                message.model_copy(deep=True) for message in history
            )

    async def finish_turn(
        self,
        conversation_id: UUID,
        *,
        run_id: UUID,
        status: RunStatus,
        assistant_message: str | None = None,
    ) -> ConversationTurn:
        if status in {RunStatus.CREATED, RunStatus.RUNNING}:
            raise ValueError("Conversation Turn cannot finish in an active Run state")
        async with self._lock:
            conversation = self._get_locked(conversation_id)
            turns = self._turns[conversation_id]
            index = next(
                (index for index, turn in enumerate(turns) if turn.run_id == run_id),
                None,
            )
            if index is None:
                raise ConversationTurnNotFoundError(
                    f"conversation '{conversation_id}' has no turn for run '{run_id}'"
                )
            if conversation.active_run_id != run_id:
                raise ConversationBusyError(
                    f"run '{run_id}' is not the active run for conversation "
                    f"'{conversation_id}'"
                )
            normalized_message = assistant_message
            if status is RunStatus.COMPLETED and normalized_message is None:
                normalized_message = ""
            updated = turns[index].model_copy(
                update={
                    "status": status,
                    "assistant_message": normalized_message,
                    "updated_at": utc_now(),
                },
                deep=True,
            )
            turns[index] = updated
            if status is not RunStatus.WAITING:
                self._conversations[conversation_id] = conversation.model_copy(
                    update={"active_run_id": None, "updated_at": utc_now()},
                    deep=True,
                )
            return updated.model_copy(deep=True)

    async def list_turns(self, conversation_id: UUID) -> tuple[ConversationTurn, ...]:
        async with self._lock:
            self._get_locked(conversation_id)
            return tuple(turn.model_copy(deep=True) for turn in self._turns[conversation_id])

    async def messages(self, conversation_id: UUID) -> tuple[Message, ...]:
        async with self._lock:
            self._get_locked(conversation_id)
            return tuple(
                message.model_copy(deep=True)
                for message in self._messages_locked(conversation_id)
            )

    def _get_locked(self, conversation_id: UUID) -> Conversation:
        try:
            return self._conversations[conversation_id]
        except KeyError as exc:
            raise ConversationNotFoundError(
                f"conversation '{conversation_id}' was not found"
            ) from exc

    def _messages_locked(self, conversation_id: UUID) -> tuple[Message, ...]:
        messages: list[Message] = []
        for turn in self._turns[conversation_id]:
            if turn.status is not RunStatus.COMPLETED:
                continue
            messages.append(Message.user(turn.user_message))
            messages.append(Message.assistant(turn.assistant_message or ""))
        return tuple(messages)


class InMemoryArtifactStore:
    """Dependency-free binary store for local runs and deterministic tests."""

    def __init__(self) -> None:
        self._attachments: dict[UUID, Attachment] = {}
        self._artifacts: dict[UUID, Artifact] = {}
        self._content: dict[UUID, bytes] = {}
        self._lock = asyncio.Lock()

    async def add_attachment(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> Attachment:
        payload = bytes(content)
        attachment = Attachment(
            name=name,
            media_type=media_type,
            size_bytes=len(payload),
            checksum_sha256=hashlib.sha256(payload).hexdigest(),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            self._attachments[attachment.id] = attachment
            self._content[attachment.id] = payload
        return attachment.model_copy(deep=True)

    async def get_attachment(self, attachment_id: UUID) -> Attachment:
        async with self._lock:
            try:
                attachment = self._attachments[attachment_id]
            except KeyError as exc:
                raise AttachmentNotFoundError(
                    f"attachment '{attachment_id}' was not found"
                ) from exc
            return attachment.model_copy(deep=True)

    async def create_artifact(
        self,
        run_id: UUID,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        payload = bytes(content)
        artifact = Artifact(
            run_id=run_id,
            name=name,
            media_type=media_type,
            size_bytes=len(payload),
            checksum_sha256=hashlib.sha256(payload).hexdigest(),
            metadata=dict(metadata or {}),
        )
        async with self._lock:
            self._artifacts[artifact.id] = artifact
            self._content[artifact.id] = payload
        return artifact.model_copy(deep=True)

    async def get_artifact(self, artifact_id: UUID) -> Artifact:
        async with self._lock:
            try:
                artifact = self._artifacts[artifact_id]
            except KeyError as exc:
                raise ArtifactNotFoundError(f"artifact '{artifact_id}' was not found") from exc
            return artifact.model_copy(deep=True)

    async def read(self, content_id: UUID) -> bytes:
        async with self._lock:
            try:
                return bytes(self._content[content_id])
            except KeyError as exc:
                raise ArtifactNotFoundError(f"content '{content_id}' was not found") from exc

    async def list_artifacts(self, run_id: UUID) -> tuple[Artifact, ...]:
        async with self._lock:
            return tuple(
                artifact.model_copy(deep=True)
                for artifact in self._artifacts.values()
                if artifact.run_id == run_id
            )

if TYPE_CHECKING:
    from base_agent.runtime.checkpoint import RuntimeCheckpoint


class InMemoryCheckpointStore:
    """Atomic in-memory checkpoint claims for local execution and tests."""

    def __init__(self) -> None:
        self._checkpoints: dict[UUID, RuntimeCheckpoint] = {}
        self._lock = asyncio.Lock()

    async def save(self, checkpoint: RuntimeCheckpoint) -> None:
        async with self._lock:
            self._checkpoints[checkpoint.run_id] = checkpoint.model_copy(deep=True)

    async def load(self, run_id: UUID) -> RuntimeCheckpoint:
        async with self._lock:
            try:
                checkpoint = self._checkpoints[run_id]
            except KeyError as exc:
                raise CheckpointNotFoundError(
                    f"checkpoint for run '{run_id}' was not found"
                ) from exc
            return checkpoint.model_copy(deep=True)

    async def claim(self, run_id: UUID) -> RuntimeCheckpoint:
        async with self._lock:
            try:
                checkpoint = self._checkpoints.pop(run_id)
            except KeyError as exc:
                raise CheckpointNotFoundError(
                    f"checkpoint for run '{run_id}' was not found or was already claimed"
                ) from exc
            return checkpoint.model_copy(deep=True)

    async def delete(self, run_id: UUID) -> None:
        async with self._lock:
            self._checkpoints.pop(run_id, None)


class InMemoryRunStore:
    def __init__(self) -> None:
        self._runs: dict[UUID, Run] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: Run) -> None:
        async with self._lock:
            if run.id in self._runs:
                raise RunAlreadyExistsError(f"run '{run.id}' already exists")
            self._runs[run.id] = run.model_copy(deep=True)

    async def get(self, run_id: UUID) -> Run:
        async with self._lock:
            try:
                run = self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(f"run '{run_id}' was not found") from exc
            return run.model_copy(deep=True)

    async def save(self, run: Run) -> None:
        async with self._lock:
            if run.id not in self._runs:
                raise RunNotFoundError(f"run '{run.id}' was not found")
            self._runs[run.id] = run.model_copy(deep=True)

    async def request_cancel(self, run_id: UUID) -> Run:
        async with self._lock:
            try:
                run = self._runs[run_id]
            except KeyError as exc:
                raise RunNotFoundError(f"run '{run_id}' was not found") from exc
            if run.status not in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING}:
                raise RunNotCancellableError(
                    f"run '{run_id}' in state '{run.status.value}' cannot be cancelled"
                )
            updated = run.model_copy(
                update={"cancel_requested": True, "updated_at": utc_now()},
                deep=True,
            )
            self._runs[run_id] = updated
            return updated.model_copy(deep=True)

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        return (await self.get(run_id)).cancel_requested


class InMemoryEventStore:
    def __init__(self) -> None:
        self._events: dict[UUID, list[RuntimeEvent]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._changed = asyncio.Condition(self._lock)

    async def emit(
        self,
        run_id: UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        async with self._changed:
            events = self._events[run_id]
            event = RuntimeEvent(
                run_id=run_id,
                sequence=len(events) + 1,
                type=event_type,
                data=data or {},
            )
            events.append(event)
            self._changed.notify_all()
            return event.model_copy(deep=True)

    async def list(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        async with self._lock:
            return tuple(event.model_copy(deep=True) for event in self._events[run_id])

    async def subscribe(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[RuntimeEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        next_sequence = after_sequence + 1
        terminal_types = {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
            EventType.RUN_LIMIT_REACHED,
            EventType.RUN_WAITING,
        }
        permanent_terminal_types = terminal_types - {EventType.RUN_WAITING}
        while True:
            async with self._changed:
                while len(self._events[run_id]) < next_sequence:
                    if any(
                        event.type in permanent_terminal_types
                        for event in self._events[run_id]
                    ):
                        return
                    await self._changed.wait()
                available = self._events[run_id][next_sequence - 1 :]
                batch = tuple(event.model_copy(deep=True) for event in available)
            if not batch:
                return
            for event in batch:
                yield event
                next_sequence = event.sequence + 1
                if event.type in terminal_types:
                    return
