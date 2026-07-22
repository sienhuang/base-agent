"""Handle for one background Agent execution."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID

from base_agent.models import AgentResult, EventType, Run, RunStatus, RuntimeEvent
from base_agent.models.run import utc_now
from base_agent.stores import CheckpointStore, EventStore, EventStream, RunStore


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
) -> Run:
    """Cancel active work, immediately finalizing a suspended Run."""
    existing = await run_store.get(run_id)
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
    await run_store.save(cancelled)
    await checkpoint_store.delete(run_id)
    await event_store.emit(
        run_id,
        EventType.RUN_CANCELLED,
        {"error": cancelled.error, "while_waiting": True},
    )
    return cancelled
