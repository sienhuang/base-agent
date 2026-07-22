"""Application-scoped retention of background Run handles."""

import asyncio
from collections.abc import Iterable
from uuid import UUID

from base_agent.agent import Agent
from base_agent.models import AgentResult, Attachment, ExecutionPlan
from base_agent.run_handle import RunHandle


class RunTaskManager:
    """Keep background tasks alive without a process-global registry."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._handles: dict[UUID, RunHandle] = {}
        self._watchers: set[asyncio.Task[None]] = set()
        self._operations: set[asyncio.Task[AgentResult]] = set()

    @property
    def active_run_ids(self) -> tuple[UUID, ...]:
        return tuple(self._handles)

    async def start(
        self,
        prompt: str,
        *,
        skills: Iterable[str] = (),
        attachments: Iterable[Attachment] = (),
        plan: ExecutionPlan | None = None,
    ) -> RunHandle:
        handle = await self.agent.start(
            prompt,
            skills=skills,
            attachments=attachments,
            plan=plan,
        )
        self._handles[handle.run_id] = handle
        watcher = asyncio.create_task(
            self._watch(handle), name=f"base-agent-server-watch-{handle.run_id}"
        )
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)
        return handle

    async def close(self) -> None:
        """Stop local watcher tasks without cancelling the underlying Runs."""

        watchers = tuple(self._watchers)
        for watcher in watchers:
            watcher.cancel()
        if watchers:
            await asyncio.gather(*watchers, return_exceptions=True)
        self._watchers.clear()

    async def resume(self, run_id: UUID, user_input: str) -> AgentResult:
        """Shield resume from request cancellation and retain its Task until completion."""

        operation = asyncio.create_task(
            self.agent.resume(run_id, user_input),
            name=f"base-agent-server-resume-{run_id}",
        )
        self._operations.add(operation)
        operation.add_done_callback(self._operation_done)
        return await asyncio.shield(operation)

    async def _watch(self, handle: RunHandle) -> None:
        try:
            await handle.result()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Run state/events are the durable error surface; watcher failure is not re-raised.
            pass
        finally:
            self._handles.pop(handle.run_id, None)

    def _operation_done(self, operation: asyncio.Task[AgentResult]) -> None:
        self._operations.discard(operation)
        if not operation.cancelled():
            operation.exception()
