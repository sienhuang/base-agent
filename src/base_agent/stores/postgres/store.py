"""Durable PostgreSQL implementations of the core Store protocols."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from typing import Any, Self, overload
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

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
from base_agent.runtime.checkpoint import RuntimeCheckpoint
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
from base_agent.stores.postgres.schema import PostgresTables, build_tables


class PostgresStore:
    """One async PostgreSQL adapter implementing all durable core Store ports."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        schema: str | None = None,
        poll_interval: float = 0.1,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be greater than zero")
        self.engine = engine
        self.tables: PostgresTables = build_tables(schema)
        self.poll_interval = poll_interval
        self._owns_engine = False

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        schema: str | None = None,
        poll_interval: float = 0.1,
        **engine_options: Any,
    ) -> Self:
        engine = create_async_engine(url, **engine_options)
        store = cls(engine, schema=schema, poll_interval=poll_interval)
        store._owns_engine = True
        return store

    async def create_schema(self) -> None:
        """Create missing tables; production deployments should use migrations."""

        async with self.engine.begin() as connection:
            await connection.run_sync(self.tables.metadata.create_all)

    async def close(self) -> None:
        if self._owns_engine:
            await self.engine.dispose()

    async def create_conversation(self, conversation: Conversation) -> None:
        statement = insert(self.tables.conversations).values(
            **_conversation_values(conversation)
        )
        try:
            async with self.engine.begin() as connection:
                await connection.execute(statement)
        except IntegrityError as exc:
            raise ConversationAlreadyExistsError(
                f"conversation '{conversation.id}' already exists"
            ) from exc

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        statement = select(self.tables.conversations.c.payload).where(
            self.tables.conversations.c.id == conversation_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise ConversationNotFoundError(
                f"conversation '{conversation_id}' was not found"
            )
        return Conversation.model_validate(payload)

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
        async with self.engine.begin() as connection:
            payload = (
                await connection.execute(
                    select(self.tables.conversations.c.payload)
                    .where(self.tables.conversations.c.id == conversation_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if payload is None:
                raise ConversationNotFoundError(
                    f"conversation '{conversation_id}' was not found"
                )
            conversation = Conversation.model_validate(payload)
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
            turn_payloads = (
                await connection.execute(
                    select(self.tables.conversation_turns.c.payload)
                    .where(
                        self.tables.conversation_turns.c.conversation_id
                        == conversation_id
                    )
                    .order_by(self.tables.conversation_turns.c.sequence)
                )
            ).scalars().all()
            history = _conversation_messages(
                tuple(ConversationTurn.model_validate(item) for item in turn_payloads)
            )
            turn = ConversationTurn(
                conversation_id=conversation_id,
                sequence=conversation.version + 1,
                run_id=run_id,
                user_message=user_message,
            )
            updated_conversation = conversation.model_copy(
                update={
                    "version": turn.sequence,
                    "active_run_id": run_id,
                    "updated_at": utc_now(),
                },
                deep=True,
            )
            await connection.execute(
                insert(self.tables.conversation_turns).values(**_turn_values(turn))
            )
            await connection.execute(
                update(self.tables.conversations)
                .where(self.tables.conversations.c.id == conversation_id)
                .values(**_conversation_values(updated_conversation))
            )
        return turn, history

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
        async with self.engine.begin() as connection:
            conversation_payload = (
                await connection.execute(
                    select(self.tables.conversations.c.payload)
                    .where(self.tables.conversations.c.id == conversation_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if conversation_payload is None:
                raise ConversationNotFoundError(
                    f"conversation '{conversation_id}' was not found"
                )
            conversation = Conversation.model_validate(conversation_payload)
            turn_payload = (
                await connection.execute(
                    select(self.tables.conversation_turns.c.payload)
                    .where(
                        self.tables.conversation_turns.c.conversation_id
                        == conversation_id,
                        self.tables.conversation_turns.c.run_id == run_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if turn_payload is None:
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
            turn = ConversationTurn.model_validate(turn_payload).model_copy(
                update={
                    "status": status,
                    "assistant_message": normalized_message,
                    "updated_at": utc_now(),
                },
                deep=True,
            )
            await connection.execute(
                update(self.tables.conversation_turns)
                .where(
                    self.tables.conversation_turns.c.conversation_id == conversation_id,
                    self.tables.conversation_turns.c.run_id == run_id,
                )
                .values(**_turn_values(turn))
            )
            if status is not RunStatus.WAITING:
                updated_conversation = conversation.model_copy(
                    update={"active_run_id": None, "updated_at": utc_now()},
                    deep=True,
                )
                await connection.execute(
                    update(self.tables.conversations)
                    .where(self.tables.conversations.c.id == conversation_id)
                    .values(**_conversation_values(updated_conversation))
                )
        return turn

    async def list_turns(
        self, conversation_id: UUID
    ) -> tuple[ConversationTurn, ...]:
        await self.get_conversation(conversation_id)
        statement = (
            select(self.tables.conversation_turns.c.payload)
            .where(self.tables.conversation_turns.c.conversation_id == conversation_id)
            .order_by(self.tables.conversation_turns.c.sequence)
        )
        async with self.engine.connect() as connection:
            payloads = (await connection.execute(statement)).scalars().all()
        return tuple(ConversationTurn.model_validate(payload) for payload in payloads)

    async def messages(self, conversation_id: UUID) -> tuple[Message, ...]:
        return _conversation_messages(await self.list_turns(conversation_id))

    async def create(self, run: Run) -> None:
        statement = insert(self.tables.runs).values(**_run_values(run))
        try:
            async with self.engine.begin() as connection:
                await connection.execute(statement)
        except IntegrityError as exc:
            raise RunAlreadyExistsError(f"run '{run.id}' already exists") from exc

    async def get(self, run_id: UUID) -> Run:
        statement = select(self.tables.runs.c.payload).where(self.tables.runs.c.id == run_id)
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise RunNotFoundError(f"run '{run_id}' was not found")
        return Run.model_validate(payload)

    @overload
    async def save(self, item: Run) -> None: ...

    @overload
    async def save(self, item: RuntimeCheckpoint) -> None: ...

    async def save(self, item: Run | RuntimeCheckpoint) -> None:
        if isinstance(item, RuntimeCheckpoint):
            await self._save_checkpoint(item)
            return
        await self._save_run(item)

    async def _save_run(self, run: Run) -> None:
        async with self.engine.begin() as connection:
            payload = (
                await connection.execute(
                    select(self.tables.runs.c.payload)
                    .where(self.tables.runs.c.id == run.id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if payload is None:
                raise RunNotFoundError(f"run '{run.id}' was not found")
            current = Run.model_validate(payload)
            # Serialize snapshot saves with request_cancel() and preserve the
            # cancellation signal once accepted. Both payload and the indexed
            # cancel_requested column must receive the same merged Run.
            updated = (
                run.model_copy(update={"cancel_requested": True}, deep=True)
                if current.cancel_requested and not run.cancel_requested
                else run
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.id == run.id)
                .values(**_run_values(updated))
            )

    async def request_cancel(self, run_id: UUID) -> Run:
        async with self.engine.begin() as connection:
            statement = (
                select(self.tables.runs.c.payload)
                .where(self.tables.runs.c.id == run_id)
                .with_for_update()
            )
            payload = (await connection.execute(statement)).scalar_one_or_none()
            if payload is None:
                raise RunNotFoundError(f"run '{run_id}' was not found")
            run = Run.model_validate(payload)
            if run.status not in {RunStatus.CREATED, RunStatus.RUNNING, RunStatus.WAITING}:
                raise RunNotCancellableError(
                    f"run '{run_id}' in state '{run.status.value}' cannot be cancelled"
                )
            updated = run.model_copy(
                update={"cancel_requested": True, "updated_at": utc_now()}, deep=True
            )
            await connection.execute(
                update(self.tables.runs)
                .where(self.tables.runs.c.id == run_id)
                .values(**_run_values(updated))
            )
        return updated

    async def is_cancel_requested(self, run_id: UUID) -> bool:
        statement = select(self.tables.runs.c.cancel_requested).where(
            self.tables.runs.c.id == run_id
        )
        async with self.engine.connect() as connection:
            requested = (await connection.execute(statement)).scalar_one_or_none()
        if requested is None:
            raise RunNotFoundError(f"run '{run_id}' was not found")
        return bool(requested)

    async def emit(
        self,
        run_id: UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        async with self.engine.begin() as connection:
            locked_run = (
                await connection.execute(
                    select(self.tables.runs.c.id)
                    .where(self.tables.runs.c.id == run_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if locked_run is None:
                raise RunNotFoundError(f"run '{run_id}' was not found")
            maximum = (
                await connection.execute(
                    select(func.max(self.tables.events.c.sequence)).where(
                        self.tables.events.c.run_id == run_id
                    )
                )
            ).scalar_one_or_none()
            event = RuntimeEvent(
                run_id=run_id,
                sequence=int(maximum or 0) + 1,
                type=event_type,
                data=data or {},
            )
            await connection.execute(
                insert(self.tables.events).values(
                    run_id=run_id,
                    sequence=event.sequence,
                    event_id=event.id,
                    event_type=event.type.value,
                    created_at=event.timestamp,
                    payload=_model_payload(event),
                )
            )
        return event.model_copy(deep=True)

    async def list(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        statement = (
            select(self.tables.events.c.payload)
            .where(self.tables.events.c.run_id == run_id)
            .order_by(self.tables.events.c.sequence)
        )
        async with self.engine.connect() as connection:
            payloads = (await connection.execute(statement)).scalars().all()
        return tuple(RuntimeEvent.model_validate(payload) for payload in payloads)

    async def subscribe(
        self,
        run_id: UUID,
        *,
        after_sequence: int = 0,
    ) -> AsyncIterator[RuntimeEvent]:
        if after_sequence < 0:
            raise ValueError("after_sequence must be non-negative")
        next_sequence = after_sequence + 1
        permanent_statuses = {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.LIMIT_REACHED,
        }
        terminal_events = {
            EventType.RUN_COMPLETED,
            EventType.RUN_FAILED,
            EventType.RUN_CANCELLED,
            EventType.RUN_LIMIT_REACHED,
            EventType.RUN_WAITING,
        }
        while True:
            events = await self.list(run_id)
            batch = tuple(event for event in events if event.sequence >= next_sequence)
            for event in batch:
                yield event
                next_sequence = event.sequence + 1
                if event.type in terminal_events:
                    return
            if (await self.get(run_id)).status in permanent_statuses:
                return
            await asyncio.sleep(self.poll_interval)

    async def _save_checkpoint(self, checkpoint: RuntimeCheckpoint) -> None:
        statement = postgresql_insert(self.tables.checkpoints).values(
            run_id=checkpoint.run_id,
            payload=_model_payload(checkpoint),
        )
        statement = statement.on_conflict_do_update(
            index_elements=[self.tables.checkpoints.c.run_id],
            set_={"payload": statement.excluded.payload},
        )
        async with self.engine.begin() as connection:
            await connection.execute(statement)

    async def load(self, run_id: UUID) -> RuntimeCheckpoint:
        statement = select(self.tables.checkpoints.c.payload).where(
            self.tables.checkpoints.c.run_id == run_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise CheckpointNotFoundError(f"checkpoint for run '{run_id}' was not found")
        return RuntimeCheckpoint.model_validate(payload)

    async def claim(self, run_id: UUID) -> RuntimeCheckpoint:
        statement = (
            delete(self.tables.checkpoints)
            .where(self.tables.checkpoints.c.run_id == run_id)
            .returning(self.tables.checkpoints.c.payload)
        )
        async with self.engine.begin() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise CheckpointNotFoundError(
                f"checkpoint for run '{run_id}' was not found or was already claimed"
            )
        return RuntimeCheckpoint.model_validate(payload)

    async def delete(self, run_id: UUID) -> None:
        async with self.engine.begin() as connection:
            await connection.execute(
                delete(self.tables.checkpoints).where(
                    self.tables.checkpoints.c.run_id == run_id
                )
            )

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
        async with self.engine.begin() as connection:
            await connection.execute(
                insert(self.tables.attachments).values(
                    id=attachment.id,
                    created_at=attachment.created_at,
                    payload=_model_payload(attachment),
                    content=payload,
                )
            )
        return attachment

    async def get_attachment(self, attachment_id: UUID) -> Attachment:
        statement = select(self.tables.attachments.c.payload).where(
            self.tables.attachments.c.id == attachment_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise AttachmentNotFoundError(f"attachment '{attachment_id}' was not found")
        return Attachment.model_validate(payload)

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
        async with self.engine.begin() as connection:
            exists = (
                await connection.execute(
                    select(self.tables.runs.c.id).where(self.tables.runs.c.id == run_id)
                )
            ).scalar_one_or_none()
            if exists is None:
                raise RunNotFoundError(f"run '{run_id}' was not found")
            await connection.execute(
                insert(self.tables.artifacts).values(
                    id=artifact.id,
                    run_id=run_id,
                    created_at=artifact.created_at,
                    payload=_model_payload(artifact),
                    content=payload,
                )
            )
        return artifact

    async def get_artifact(self, artifact_id: UUID) -> Artifact:
        statement = select(self.tables.artifacts.c.payload).where(
            self.tables.artifacts.c.id == artifact_id
        )
        async with self.engine.connect() as connection:
            payload = (await connection.execute(statement)).scalar_one_or_none()
        if payload is None:
            raise ArtifactNotFoundError(f"artifact '{artifact_id}' was not found")
        return Artifact.model_validate(payload)

    async def read(self, content_id: UUID) -> bytes:
        async with self.engine.connect() as connection:
            content = (
                await connection.execute(
                    select(self.tables.attachments.c.content).where(
                        self.tables.attachments.c.id == content_id
                    )
                )
            ).scalar_one_or_none()
            if content is None:
                content = (
                    await connection.execute(
                        select(self.tables.artifacts.c.content).where(
                            self.tables.artifacts.c.id == content_id
                        )
                    )
                ).scalar_one_or_none()
        if content is None:
            raise ArtifactNotFoundError(f"content '{content_id}' was not found")
        return bytes(content)

    async def list_artifacts(self, run_id: UUID) -> tuple[Artifact, ...]:
        statement = (
            select(self.tables.artifacts.c.payload)
            .where(self.tables.artifacts.c.run_id == run_id)
            .order_by(self.tables.artifacts.c.created_at, self.tables.artifacts.c.id)
        )
        async with self.engine.connect() as connection:
            payloads = (await connection.execute(statement)).scalars().all()
        return tuple(Artifact.model_validate(payload) for payload in payloads)


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def _run_values(run: Run) -> dict[str, Any]:
    return {
        "id": run.id,
        "status": run.status.value,
        "cancel_requested": run.cancel_requested,
        "updated_at": run.updated_at,
        "payload": _model_payload(run),
    }


def _conversation_values(conversation: Conversation) -> dict[str, Any]:
    return {
        "id": conversation.id,
        "profile_id": conversation.profile_id,
        "version": conversation.version,
        "active_run_id": conversation.active_run_id,
        "updated_at": conversation.updated_at,
        "payload": _model_payload(conversation),
    }


def _turn_values(turn: ConversationTurn) -> dict[str, Any]:
    return {
        "conversation_id": turn.conversation_id,
        "sequence": turn.sequence,
        "run_id": turn.run_id,
        "status": turn.status.value,
        "updated_at": turn.updated_at,
        "payload": _model_payload(turn),
    }


def _conversation_messages(turns: tuple[ConversationTurn, ...]) -> tuple[Message, ...]:
    messages: list[Message] = []
    for turn in turns:
        if turn.status is not RunStatus.COMPLETED:
            continue
        messages.append(Message.user(turn.user_message))
        messages.append(Message.assistant(turn.assistant_message or ""))
    return tuple(messages)
