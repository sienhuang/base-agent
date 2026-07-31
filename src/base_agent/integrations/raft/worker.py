"""Application-owned Worker that maps Raft messages onto base-agent Runs."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterable
from typing import Protocol
from uuid import UUID, uuid5

from base_agent.integrations.raft.client import RaftCliClient, RaftClient
from base_agent.integrations.raft.errors import RaftCliCommandError
from base_agent.integrations.raft.models import (
    RaftDrainResult,
    RaftMessage,
    RaftWorkerConfig,
)
from base_agent.integrations.raft.parser import parse_raft_messages
from base_agent.integrations.raft.state import (
    CachedReply,
    OpenTask,
    RaftWorkerStateStore,
)
from base_agent.integrations.raft.wake import RaftWakeServer
from base_agent.models import AgentResult, AgentResultStatus
from base_agent.stores.errors import RunNotFoundError

logger = logging.getLogger(__name__)

_RAFT_RUN_NAMESPACE = UUID("97fb75bc-ff5a-41fc-a4c3-31fb69cf6d17")
_TERMINAL_TASK_STATUSES = frozenset({"done", "closed"})


class AgentExecutor(Protocol):
    async def run(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        skills: Iterable[str] = (),
    ) -> AgentResult: ...

    async def resume(self, run_id: UUID, user_input: str) -> AgentResult: ...


class RaftWorker:
    """Safely coordinate Raft delivery around an application Agent."""

    def __init__(
        self,
        agent: AgentExecutor,
        config: RaftWorkerConfig,
        *,
        client: RaftClient | None = None,
        skills: Iterable[str] = (),
    ) -> None:
        self.agent = agent
        self.config = config
        self.client = client or RaftCliClient(config)
        self.skills = tuple(skills)
        self._state = RaftWorkerStateStore(
            config.profile_state_dir,
            completed_limit=config.processed_message_limit,
        )
        self._drain_lock = asyncio.Lock()
        self._mention = re.compile(
            rf"(?<![\w-])@{re.escape(config.handle)}(?![\w-])",
            re.IGNORECASE,
        )

    async def run_once(self) -> RaftDrainResult:
        """Drain and handle currently pending messages once."""
        async with self._drain_lock:
            raw = self._state.read_spool()
            if raw is None:
                batch = await self.client.check_messages()
                if not batch.messages:
                    return RaftDrainResult(received=0, handled=0, skipped=0)
                self._state.write_spool(batch.raw)
                messages = batch.messages
            else:
                messages = parse_raft_messages(raw)

            handled = 0
            skipped = 0
            for message in messages:
                if self._state.is_completed(message.message_id):
                    skipped += 1
                    continue
                if await self._process_message(message):
                    handled += 1
                else:
                    skipped += 1
            self._state.clear_spool()
            return RaftDrainResult(
                received=len(messages),
                handled=handled,
                skipped=skipped,
            )

    async def run_forever(self) -> None:
        """Run the official wake bridge and consume messages until cancelled."""
        wake_server = RaftWakeServer(
            profile=self.config.profile,
            agent_id=self.config.agent_id,
        )
        wake_server.start()
        bridge = await self.client.start_bridge(
            wake_endpoint=wake_server.endpoint,
            wake_token=wake_server.token,
            runtime_session=wake_server.runtime_session,
        )
        bridge_wait = asyncio.create_task(bridge.wait())
        logger.info(
            "Raft Worker started",
            extra={
                "event": "raft.worker.started",
                "profile": self.config.profile,
                "agent_id": str(self.config.agent_id),
                "bridge_pid": bridge.pid,
            },
        )
        try:
            await self.run_once()
            while True:
                wake_wait = asyncio.create_task(wake_server.wait())
                done, pending = await asyncio.wait(
                    {wake_wait, bridge_wait},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    if task is not bridge_wait:
                        task.cancel()
                if bridge_wait in done:
                    await bridge_wait
                await wake_wait
                await self.run_once()
        finally:
            if not bridge_wait.done():
                bridge_wait.cancel()
            await bridge.close()
            await wake_server.close()
            logger.info(
                "Raft Worker stopped",
                extra={
                    "event": "raft.worker.stopped",
                    "profile": self.config.profile,
                    "agent_id": str(self.config.agent_id),
                },
            )

    async def _process_message(self, message: RaftMessage) -> bool:
        conversation_key = _conversation_key(message)
        if not self._should_handle(message, conversation_key):
            self._state.mark_completed(message.message_id)
            return False

        open_task = self._task_for_message(message, conversation_key)
        if message.task_number is not None and not await self._claim_if_needed(message):
            self._state.mark_completed(message.message_id)
            return False

        cached = self._state.state.cached_replies.get(message.message_id)
        if cached is None:
            result = await self._execute(message, conversation_key)
            open_task = self._task_for_message(message, conversation_key)
            cached = self._cache_reply(
                message,
                result,
                conversation_key=conversation_key,
                open_task=open_task,
            )

        if message.message_id not in self._state.state.replied_ids:
            await self.client.send_message(cached.target, cached.content)
            self._state.state.replied_ids.add(message.message_id)
            self._state.save()

        if (
            cached.move_task_to_review
            and cached.task_target is not None
            and cached.task_number is not None
        ):
            await self.client.update_task(
                cached.task_target,
                cached.task_number,
                "in_review",
            )
            self._state.state.open_tasks.pop(conversation_key, None)
            self._state.state.waiting_runs.pop(conversation_key, None)
            self._state.save()

        self._state.mark_completed(message.message_id)
        return True

    def _should_handle(
        self,
        message: RaftMessage,
        conversation_key: str,
    ) -> bool:
        if message.sender_handle.casefold() == self.config.handle.casefold():
            return False
        if message.sender_type in {"system", "third_party_app"}:
            return False
        if message.task_status in _TERMINAL_TASK_STATUSES:
            return False
        if (
            message.task_assignee_id is not None
            and message.task_assignee_id != str(self.config.agent_id)
        ):
            return False
        if conversation_key in self._state.state.waiting_runs:
            return True
        if message.is_direct_message:
            return True
        if message.task_assignee_id == str(self.config.agent_id):
            return True
        return self._mention.search(message.content) is not None

    async def _claim_if_needed(self, message: RaftMessage) -> bool:
        assert message.task_number is not None
        if message.message_id in self._state.state.claimed_ids:
            return True
        already_claimed_by_self = (
            message.task_status == "in_progress"
            and message.task_assignee_id == str(self.config.agent_id)
        )
        if not already_claimed_by_self:
            try:
                await self.client.claim_task(
                    _task_target(message.target),
                    message.task_number,
                )
            except RaftCliCommandError:
                logger.warning(
                    "Raft task claim failed; skipping conflicting work",
                    extra={
                        "event": "raft.task.claim_failed",
                        "message_id": message.message_id,
                        "task_number": message.task_number,
                    },
                )
                return False
        self._state.state.claimed_ids.add(message.message_id)
        self._state.save()
        return True

    async def _execute(
        self,
        message: RaftMessage,
        conversation_key: str,
    ) -> AgentResult:
        waiting_run = self._state.state.waiting_runs.get(conversation_key)
        if waiting_run is not None:
            try:
                return await self.agent.resume(UUID(waiting_run), message.content)
            except (RunNotFoundError, ValueError):
                self._state.state.waiting_runs.pop(conversation_key, None)
                self._state.state.open_tasks.pop(conversation_key, None)
                self._state.save()
        run_id = uuid5(
            _RAFT_RUN_NAMESPACE,
            f"{self.config.agent_id}:{message.message_id}",
        )
        return await self.agent.run(
            _build_prompt(message),
            run_id=run_id,
            skills=self.skills,
        )

    def _cache_reply(
        self,
        message: RaftMessage,
        result: AgentResult,
        *,
        conversation_key: str,
        open_task: OpenTask | None,
    ) -> CachedReply:
        if result.status is AgentResultStatus.WAITING:
            run_id = str(result.metadata["run_id"])
            self._state.state.waiting_runs[conversation_key] = run_id
            if open_task is not None:
                self._state.state.open_tasks[conversation_key] = open_task
        else:
            self._state.state.waiting_runs.pop(conversation_key, None)

        completed = result.status is AgentResultStatus.COMPLETED
        reply = CachedReply(
            target=_reply_target(message, open_task),
            content=_bounded_reply(result, self.config.max_reply_chars),
            move_task_to_review=completed and open_task is not None,
            task_target=open_task.target if open_task is not None else None,
            task_number=open_task.number if open_task is not None else None,
        )
        self._state.state.cached_replies[message.message_id] = reply
        self._state.save()
        return reply

    def _task_for_message(
        self,
        message: RaftMessage,
        conversation_key: str,
    ) -> OpenTask | None:
        if message.task_number is not None:
            return OpenTask(
                target=_task_target(message.target),
                number=message.task_number,
            )
        return self._state.state.open_tasks.get(conversation_key)


def _build_prompt(message: RaftMessage) -> str:
    task_context = (
        f"Raft task #{message.task_number} ({message.task_status}).\n"
        if message.task_number is not None
        else ""
    )
    return (
        "You are responding through a Raft External Agent Worker. "
        "Do not invoke Raft CLI commands or attempt to send the response yourself; "
        "the Worker owns task claims, transport, and status updates.\n"
        f"Sender: @{message.sender_handle}\n"
        f"Source: {message.target}\n"
        f"{task_context}"
        "Request:\n"
        f"{message.content}"
    )


def _bounded_reply(result: AgentResult, limit: int) -> str:
    if result.status is AgentResultStatus.COMPLETED:
        text = result.output or "已完成，但没有文本输出。"
    elif result.status is AgentResultStatus.WAITING:
        text = result.output or "需要更多信息才能继续，请在当前线程回复。"
    else:
        detail = result.error or result.output or result.status.value
        text = f"处理未完成（{result.status.value}）：{detail}"
    if len(text) <= limit:
        return text
    marker = "\n\n[结果已截断；完整内容保留在本地 Run/Artifact 中。]"
    return f"{text[: max(0, limit - len(marker))]}{marker}"


def _conversation_key(message: RaftMessage) -> str:
    if message.is_direct_message or message.is_thread:
        return message.target
    return f"{message.target}:{message.message_id}"


def _task_target(target: str) -> str:
    if target.startswith("#") and ":" in target:
        return target.split(":", 1)[0]
    return target


def _reply_target(
    message: RaftMessage,
    open_task: OpenTask | None,
) -> str:
    if (
        open_task is not None
        and message.target.startswith("#")
        and not message.is_thread
    ):
        return f"{message.target}:{message.message_id}"
    return message.target
