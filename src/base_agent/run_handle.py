"""Handle for one background Agent execution."""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from base_agent._logging import reset_log_context, set_log_context
from base_agent.models import AgentResult, EventType, Run, RunStatus, RuntimeEvent
from base_agent.models.run import utc_now
from base_agent.stores import (
    CheckpointStore,
    ConversationStore,
    EventStore,
    EventStream,
    RunStore,
)
from base_agent.stores.errors import RunNotFoundError

logger = logging.getLogger(__name__)


class EventStreamingNotSupportedError(RuntimeError):
    """The configured EventStore does not expose live subscriptions."""


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Control and observe a Run without coupling it to an HTTP or queue implementation."""

    run_id: UUID
    _task: asyncio.Task[AgentResult]
    _run_store: RunStore
    _event_store: EventStore
    _checkpoint_store: CheckpointStore
    _conversation_store: ConversationStore | None = None

    @property
    def done(self) -> bool:
        return self._task.done()

    async def result(self) -> AgentResult:
        """Wait for completion without cancelling the Run if this waiter is cancelled."""
        return await asyncio.shield(self._task)

    async def cancel(self) -> Run:
        """Request cooperative cancellation through the configured RunStore."""
        return await request_cancellation(
            self.run_id,
            run_store=self._run_store,
            event_store=self._event_store,
            checkpoint_store=self._checkpoint_store,
            conversation_store=self._conversation_store,
        )

    async def get_run(self) -> Run:
        return await self._run_store.get(self.run_id)

    async def events(self) -> tuple[RuntimeEvent, ...]:
        return await self._event_store.list(self.run_id)

    def stream(self, *, after_sequence: int = 0) -> AsyncIterator[RuntimeEvent]:
        """Replay from a cursor and then follow new events through the terminal boundary."""
        if not isinstance(self._event_store, EventStream):
            raise EventStreamingNotSupportedError(
                "the configured EventStore does not support live subscriptions"
            )
        return self._event_store.subscribe(self.run_id, after_sequence=after_sequence)


async def request_cancellation(
    run_id: UUID,
    *,
    run_store: RunStore,
    event_store: EventStore,
    checkpoint_store: CheckpointStore,
    conversation_store: ConversationStore | None = None,
) -> Run:
    """Cancel active work, immediately finalizing a suspended Run."""
    existing = await run_store.get(run_id)
    log_tokens = set_log_context(
        run_id=run_id,
        conversation_id=existing.conversation_id,
        turn_sequence=existing.turn_sequence,
    )
    logger.info(
        "run cancellation requested",
        extra={
            "event": "run.cancellation_requested",
            "status": existing.status.value,
        },
    )
    try:
        requested = await run_store.request_cancel(run_id)
        if existing.status is not RunStatus.WAITING:
            return requested
        cancelled = requested.model_copy(
            update={
                "status": RunStatus.CANCELLED,
                "error": "run cancellation requested while waiting for input",
                "updated_at": utc_now(),
            },
            deep=True,
        )
        if cancelled.conversation_id is not None and conversation_store is not None:
            await conversation_store.finish_turn(
                cancelled.conversation_id,
                run_id=run_id,
                status=RunStatus.CANCELLED,
            )
        await run_store.save(cancelled)
        await checkpoint_store.delete(run_id)
        await event_store.emit(
            run_id,
            EventType.RUN_CANCELLED,
            {"error": cancelled.error, "while_waiting": True},
        )
        logger.info(
            "waiting run cancelled",
            extra={"event": "run.cancelled", "status": cancelled.status.value},
        )
        return cancelled
    finally:
        reset_log_context(log_tokens)


async def finalize_task_interruption(
    run_id: UUID,
    *,
    run_store: RunStore,
    event_store: EventStore,
    checkpoint_store: CheckpointStore,
    conversation_store: ConversationStore | None = None,
) -> Run | None:
    """Finalize an asyncio task interruption without treating it as a user request."""

    try:
        existing = await run_store.get(run_id)
    except RunNotFoundError:
        return None
    if existing.status not in {
        RunStatus.CREATED,
        RunStatus.RUNNING,
        RunStatus.WAITING,
    }:
        return existing

    requested = existing.cancel_requested
    status = RunStatus.CANCELLED if requested else RunStatus.INTERRUPTED
    error = "run cancellation requested" if requested else "runtime task interrupted"
    metadata = dict(existing.metadata)
    if not requested:
        metadata["interruption"] = {
            "source": "asyncio_task_cancelled",
            "recoverable": False,
        }
    finalized = existing.model_copy(
        update={
            "status": status,
            "error": error,
            "metadata": metadata,
            "updated_at": utc_now(),
        },
        deep=True,
    )
    if finalized.conversation_id is not None and conversation_store is not None:
        await conversation_store.finish_turn(
            finalized.conversation_id,
            run_id=run_id,
            status=status,
        )
    await run_store.save(finalized)
    await checkpoint_store.delete(run_id)
    event_type = (
        EventType.RUN_CANCELLED if requested else EventType.RUN_INTERRUPTED
    )
    event_data: dict[str, object] = {"error": error}
    if requested:
        event_data["cancel_source"] = "requested"
    else:
        event_data.update(
            {
                "interruption_source": "asyncio_task_cancelled",
                "recoverable": False,
            }
        )
    await event_store.emit(run_id, event_type, event_data)
    return finalized
