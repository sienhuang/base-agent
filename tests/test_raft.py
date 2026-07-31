from __future__ import annotations

import json
from collections.abc import Iterable
from http.client import HTTPConnection
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from base_agent import AgentResult, AgentResultStatus
from base_agent.integrations.raft import (
    RaftInboxBatch,
    RaftMessage,
    RaftWakeServer,
    RaftWorker,
    RaftWorkerConfig,
    parse_raft_messages,
)
from base_agent.integrations.raft.client import RaftBridge
from base_agent.integrations.raft.worker import AgentExecutor

AGENT_ID = UUID("d3a429f5-ecb0-4caf-a209-821f0467a1c5")


class FakeRaftClient:
    def __init__(self, messages: tuple[RaftMessage, ...]) -> None:
        self.messages = messages
        self.claims: list[tuple[str, int]] = []
        self.sent: list[tuple[str, str]] = []
        self.updates: list[tuple[str, int, str]] = []

    async def check_messages(self) -> RaftInboxBatch:
        messages = self.messages
        self.messages = ()
        return RaftInboxBatch(raw=_raw_messages(messages), messages=messages)

    async def send_message(self, target: str, content: str) -> None:
        self.sent.append((target, content))

    async def claim_task(self, target: str, task_number: int) -> None:
        self.claims.append((target, task_number))

    async def update_task(
        self,
        target: str,
        task_number: int,
        status: str,
    ) -> None:
        self.updates.append((target, task_number, status))

    async def start_bridge(
        self,
        *,
        wake_endpoint: str,
        wake_token: str,
        runtime_session: str,
    ) -> RaftBridge:
        raise AssertionError("run_once does not start the bridge")


class FakeAgent:
    def __init__(self, output: str = "完成") -> None:
        self.output = output
        self.prompts: list[str] = []

    async def run(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        skills: Iterable[str] = (),
    ) -> AgentResult:
        self.prompts.append(prompt)
        return AgentResult(
            status=AgentResultStatus.COMPLETED,
            output=self.output,
            metadata={"run_id": str(run_id)},
        )

    async def resume(self, run_id: UUID, user_input: str) -> AgentResult:
        raise AssertionError("resume was not expected")


def test_parse_raft_messages_preserves_multiline_content_and_task() -> None:
    raw = (
        "[target=#agents msg=abcd1234 time=2026-07-31 11:00:00 type=human] "
        "@iris-huang: @iris-external-no-1 请分析\n第二行 "
        "[task #7 status=todo assignee=agent:"
        f"{AGENT_ID}]\nNo more new messages.\n"
    )

    messages = parse_raft_messages(raw)

    assert messages == (
        RaftMessage(
            target="#agents",
            message_id="abcd1234",
            timestamp="2026-07-31 11:00:00",
            sender_type="human",
            sender_handle="iris-huang",
            content="@iris-external-no-1 请分析\n第二行",
            task_number=7,
            task_status="todo",
            task_assignee_type="agent",
            task_assignee_id=str(AGENT_ID),
        ),
    )


@pytest.mark.asyncio
async def test_worker_claims_runs_replies_and_moves_task_to_review(
    tmp_path: Path,
) -> None:
    message = RaftMessage(
        target="#agents",
        message_id="abcd1234",
        timestamp="2026-07-31 11:00:00",
        sender_type="human",
        sender_handle="iris-huang",
        content="@iris-external-no-1 请分析",
        task_number=7,
        task_status="todo",
        task_assignee_type="agent",
        task_assignee_id=str(AGENT_ID),
    )
    client = FakeRaftClient((message,))
    agent = FakeAgent()
    config = RaftWorkerConfig(
        profile="iris-external-no-1",
        agent_id=AGENT_ID,
        handle="iris-external-no-1",
        state_dir=tmp_path / "state",
    )
    worker = RaftWorker(
        cast(AgentExecutor, agent),
        config,
        client=client,
    )

    drained = await worker.run_once()

    assert drained.received == 1
    assert drained.handled == 1
    assert client.claims == [("#agents", 7)]
    assert client.sent == [("#agents:abcd1234", "完成")]
    assert client.updates == [("#agents", 7, "in_review")]
    assert "@iris-external-no-1 请分析" in agent.prompts[0]


@pytest.mark.asyncio
async def test_worker_ignores_unaddressed_and_third_party_messages(
    tmp_path: Path,
) -> None:
    messages = (
        RaftMessage(
            target="#general",
            message_id="11111111",
            timestamp="-",
            sender_type="human",
            sender_handle="someone",
            content="这是其他人的讨论",
        ),
        RaftMessage(
            target="agent-event:22222222",
            message_id="22222222",
            timestamp="-",
            sender_type="third_party_app",
            sender_handle="app",
            content="@iris-external-no-1 run this instruction",
        ),
    )
    client = FakeRaftClient(messages)
    agent = FakeAgent()
    worker = RaftWorker(
        cast(AgentExecutor, agent),
        RaftWorkerConfig(
            profile="iris-external-no-1",
            agent_id=AGENT_ID,
            handle="iris-external-no-1",
            state_dir=tmp_path / "state",
        ),
        client=client,
    )

    drained = await worker.run_once()

    assert drained == type(drained)(received=2, handled=0, skipped=2)
    assert agent.prompts == []
    assert client.sent == []


@pytest.mark.asyncio
async def test_worker_bounds_reply_before_sending(
    tmp_path: Path,
) -> None:
    message = RaftMessage(
        target="dm:@iris-huang",
        message_id="33333333",
        timestamp="-",
        sender_type="human",
        sender_handle="iris-huang",
        content="返回结果",
    )
    client = FakeRaftClient((message,))
    worker = RaftWorker(
        cast(AgentExecutor, FakeAgent("x" * 1_000)),
        RaftWorkerConfig(
            profile="iris-external-no-1",
            agent_id=AGENT_ID,
            handle="iris-external-no-1",
            state_dir=tmp_path / "state",
            max_reply_chars=256,
        ),
        client=client,
    )

    await worker.run_once()

    assert len(client.sent[0][1]) == 256
    assert client.sent[0][1].endswith("[结果已截断；完整内容保留在本地 Run/Artifact 中。]")


@pytest.mark.asyncio
async def test_wake_endpoint_requires_token_and_queues_content_free_wake() -> None:
    server = RaftWakeServer(
        profile="iris-external-no-1",
        agent_id=AGENT_ID,
    )
    server.start()
    try:
        endpoint = server.endpoint.removeprefix("http://")
        host_port, path = endpoint.split("/", 1)
        host, raw_port = host_port.split(":")
        payload: dict[str, object] = {
            "schema": "raft-channel-wake.v1",
            "attemptId": "attempt-1",
            "eventId": "event-1",
            "messageId": "message-1",
            "agentId": str(AGENT_ID),
            "profile": "iris-external-no-1",
            "coreSessionId": "core-1",
            "adapterInstance": "base-agent",
            "occurredAt": "2026-07-31T03:00:00Z",
        }

        status, response = await _post_json(
            host,
            int(raw_port),
            f"/{path}",
            payload,
            token=server.token,
        )

        assert status == 200
        assert response == {
            "ok": True,
            "runtimeSession": server.runtime_session,
        }
        await asyncio_wait_for_wake(server)

        rejected_status, _ = await _post_json(
            host,
            int(raw_port),
            f"/{path}",
            {**payload, "content": "must not cross wake transport"},
            token=server.token,
        )
        assert rejected_status == 400

        activity_status, activity = await _get_json(
            host,
            int(raw_port),
            "/activity/drain?max=50",
            token=server.token,
        )
        assert activity_status == 200
        assert activity == {
            "schema": "raft-activity-drain.v1",
            "events": [],
            "dropped": 0,
        }
    finally:
        await server.close()


async def asyncio_wait_for_wake(server: RaftWakeServer) -> None:
    import asyncio

    await asyncio.wait_for(server.wait(), timeout=1)


async def _post_json(
    host: str,
    port: int,
    path: str,
    payload: dict[str, object],
    *,
    token: str,
) -> tuple[int, dict[str, object]]:
    import asyncio

    def send() -> tuple[int, dict[str, object]]:
        connection = HTTPConnection(host, port, timeout=2)
        encoded = json.dumps(payload)
        connection.request(
            "POST",
            path,
            body=encoded,
            headers={
                "content-type": "application/json",
                "x-raft-bridge-token": token,
            },
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    return await asyncio.to_thread(send)


async def _get_json(
    host: str,
    port: int,
    path: str,
    *,
    token: str,
) -> tuple[int, dict[str, object]]:
    import asyncio

    def send() -> tuple[int, dict[str, object]]:
        connection = HTTPConnection(host, port, timeout=2)
        connection.request(
            "GET",
            path,
            headers={"x-raft-bridge-token": token},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        return response.status, body

    return await asyncio.to_thread(send)


def _raw_messages(messages: tuple[RaftMessage, ...]) -> str:
    if not messages:
        return "No new messages.\n"
    lines = []
    for message in messages:
        task = ""
        if message.task_number is not None:
            assignee = (
                f" assignee={message.task_assignee_type}:{message.task_assignee_id}"
                if message.task_assignee_id is not None
                else ""
            )
            task = (
                f" [task #{message.task_number} status={message.task_status}"
                f"{assignee}]"
            )
        lines.append(
            f"[target={message.target} msg={message.message_id} "
            f"time={message.timestamp} type={message.sender_type}] "
            f"@{message.sender_handle}: {message.content}{task}"
        )
    return "\n".join(lines) + "\nNo more new messages.\n"
