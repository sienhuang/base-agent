import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from base_agent import (
    Agent,
    AgentProfile,
    EventType,
    InMemoryEventStore,
    ModelRequest,
    ModelResponse,
    RunStatus,
    RuntimeEvent,
    ToolCall,
    ToolContext,
    WaitForInput,
    tool,
)
from base_agent.server import RunTaskManager, create_app
from base_agent.testing import FakeModel


@asynccontextmanager
async def server_client(agent: Agent) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    app = create_app(agent, expose_artifact_content=True)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield client, app


async def wait_for_status(
    client: httpx.AsyncClient,
    run_id: str,
    expected: RunStatus,
) -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == expected.value:
            return payload
        await asyncio.sleep(0)
    raise AssertionError(f"run did not reach {expected.value}")


@pytest.mark.asyncio
async def test_server_starts_queries_and_replays_a_completed_run() -> None:
    agent = Agent(
        profile=AgentProfile(id="server", instructions="Work."),
        model=FakeModel([ModelResponse(content="done")]),
    )

    async with server_client(agent) as (client, app):
        started = await client.post("/v1/runs", json={"prompt": "work"})
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        run = await wait_for_status(client, run_id, RunStatus.COMPLETED)
        events = await client.get(f"/v1/runs/{run_id}/events", params={"after_sequence": 3})

        assert run["output"] == "done"
        assert [event["sequence"] for event in events.json()] == [4, 5]
        assert isinstance(app.state.run_tasks, RunTaskManager)
        await asyncio.sleep(0)
        assert UUID(run_id) not in app.state.run_tasks.active_run_ids


@pytest.mark.asyncio
async def test_server_conversation_runs_share_history_through_the_normal_run_api() -> None:
    model = FakeModel(
        [
            ModelResponse(content="Hello Xiao Ming."),
            ModelResponse(content="Your name is Xiao Ming."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="conversation-server", instructions="Remember."),
        model=model,
    )

    async with server_client(agent) as (client, _):
        created = await client.post(
            "/v1/conversations",
            json={"metadata": {"tenant": "demo"}},
        )
        assert created.status_code == 201
        conversation_id = created.json()["id"]

        first = await client.post(
            "/v1/runs",
            json={
                "prompt": "My name is Xiao Ming.",
                "conversation_id": conversation_id,
            },
        )
        await wait_for_status(client, first.json()["run_id"], RunStatus.COMPLETED)
        second = await client.post(
            f"/v1/conversations/{conversation_id}/runs",
            json={"prompt": "What is my name?"},
        )
        await wait_for_status(client, second.json()["run_id"], RunStatus.COMPLETED)
        conversation = await client.get(f"/v1/conversations/{conversation_id}")
        turns = await client.get(f"/v1/conversations/{conversation_id}/turns")
        messages = await client.get(f"/v1/conversations/{conversation_id}/messages")

        assert first.status_code == 202
        assert first.json()["turn_sequence"] == 1
        assert second.status_code == 202
        assert second.json()["turn_sequence"] == 2
        assert conversation.json()["version"] == 2
        assert conversation.json()["active_run_id"] is None
        assert [turn["sequence"] for turn in turns.json()] == [1, 2]
        assert [message["content"] for message in messages.json()] == [
            "My name is Xiao Ming.",
            "Hello Xiao Ming.",
            "What is my name?",
            "Your name is Xiao Ming.",
        ]
        assert [message.content for message in model.requests[1].messages] == [
            "Remember.",
            "My name is Xiao Ming.",
            "Hello Xiao Ming.",
            "What is my name?",
        ]


@pytest.mark.asyncio
async def test_server_can_generate_and_execute_plan_in_one_run() -> None:
    model = FakeModel(
        [
            ModelResponse(
                content=(
                    '{"id":"http-plan","title":"HTTP plan","steps":['
                    '{"id":"work","description":"Do the work","dependencies":[]}]}'
                )
            ),
            ModelResponse(content="step done"),
            ModelResponse(content='{"steps":[]}'),
            ModelResponse(content="all done"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="planning-server", instructions="Work."),
        model=model,
    )

    async with server_client(agent) as (client, _):
        started = await client.post(
            "/v1/runs",
            json={"prompt": "plan and execute", "planning": True},
        )
        run = await wait_for_status(
            client,
            started.json()["run_id"],
            RunStatus.COMPLETED,
        )

    assert started.status_code == 202
    assert run["metadata"]["planning_requested"] is True
    assert run["metadata"]["plan"]["status"] == "completed"
    assert run["output"] == "all done"
    assert len(model.requests) == 4


@pytest.mark.asyncio
async def test_server_rejects_overlapping_conversation_runs() -> None:
    model = ControlledModel()
    agent = Agent(
        profile=AgentProfile(id="conversation-busy", instructions="Work."),
        model=model,
    )

    async with server_client(agent) as (client, _):
        conversation_id = (await client.post("/v1/conversations", json={})).json()["id"]
        first = await client.post(
            "/v1/runs",
            json={"prompt": "first", "conversation_id": conversation_id},
        )
        await model.started.wait()
        overlapping = await client.post(
            "/v1/runs",
            json={"prompt": "second", "conversation_id": conversation_id},
        )
        model.release.set()
        await wait_for_status(client, first.json()["run_id"], RunStatus.COMPLETED)

        assert overlapping.status_code == 409
        assert "already has active run" in overlapping.json()["detail"]


@pytest.mark.asyncio
async def test_sse_stream_supports_query_and_last_event_id_cursors() -> None:
    agent = Agent(
        profile=AgentProfile(id="sse", instructions="Work."),
        model=FakeModel([ModelResponse(content="done")]),
    )

    async with server_client(agent) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "work"})
        run_id = started.json()["run_id"]
        await wait_for_status(client, run_id, RunStatus.COMPLETED)

        replay = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            params={"after_sequence": 3},
        )
        resumed = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "4"},
        )
        exhausted = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "5"},
        )

        assert replay.status_code == 200
        assert replay.headers["content-type"].startswith("text/event-stream")
        assert "id: 4\nevent: model.responded" in replay.text
        assert "id: 5\nevent: run.completed" in replay.text
        assert "id: 4" not in resumed.text
        assert "id: 5\nevent: run.completed" in resumed.text
        assert exhausted.text == ""


@tool
async def ask_user(question: str) -> WaitForInput:
    """Ask for input."""
    return WaitForInput(prompt=question)


@pytest.mark.asyncio
async def test_server_resumes_a_waiting_run_and_reports_conflicts() -> None:
    agent = Agent(
        profile=AgentProfile(
            id="resume-server",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="ask_user",
                            arguments={"question": "Region?"},
                        ),
                    )
                ),
                ModelResponse(content="Using APAC."),
            ]
        ),
        tools=(ask_user,),
    )

    async with server_client(agent) as (client, _):
        conversation_id = (
            await client.post("/v1/conversations", json={})
        ).json()["id"]
        started = await client.post(
            "/v1/runs",
            json={"prompt": "report", "conversation_id": conversation_id},
        )
        run_id = started.json()["run_id"]
        waiting = await wait_for_status(client, run_id, RunStatus.WAITING)
        active = await client.get(f"/v1/conversations/{conversation_id}")
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume", json={"input": "APAC"}
        )
        duplicate = await client.post(
            f"/v1/runs/{run_id}/resume", json={"input": "again"}
        )

        assert waiting["metadata"]["pending_input"]["prompt"] == "Region?"
        assert active.json()["active_run_id"] == run_id
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "completed"
        assert resumed.json()["output"] == "Using APAC."
        assert duplicate.status_code == 409
        turns = (await client.get(f"/v1/conversations/{conversation_id}/turns")).json()
        conversation = (await client.get(f"/v1/conversations/{conversation_id}")).json()
        assert len(turns) == 1
        assert turns[0]["run_id"] == run_id
        assert turns[0]["status"] == "completed"
        assert conversation["active_run_id"] is None


class ControlledModel:
    name = "controlled-server"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse(content="late")


class BlockingResumeModel:
    name = "blocking-resume-server"

    def __init__(self) -> None:
        self.calls = 0
        self.resumed = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="ask_user",
                        arguments={"question": "Continue?"},
                    ),
                )
            )
        self.resumed.set()
        await self.release.wait()
        return ModelResponse(content="resumed")


@pytest.mark.asyncio
async def test_server_requests_cooperative_cancellation() -> None:
    model = ControlledModel()
    agent = Agent(
        profile=AgentProfile(id="cancel-server", instructions="Work."),
        model=model,
    )

    async with server_client(agent) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "work"})
        run_id = started.json()["run_id"]
        await model.started.wait()
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
        model.release.set()
        final = await wait_for_status(client, run_id, RunStatus.CANCELLED)

        assert cancelled.status_code == 200
        assert cancelled.json()["cancel_requested"] is True
        assert final["error"] == "run cancellation requested"


@pytest.mark.asyncio
async def test_task_manager_shields_resume_when_http_waiter_is_cancelled() -> None:
    model = BlockingResumeModel()
    agent = Agent(
        profile=AgentProfile(
            id="shield-resume",
            instructions="Ask.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )
    waiting = await agent.run("work")
    run_id = UUID(str(waiting.metadata["run_id"]))
    manager = RunTaskManager(agent)
    waiter = asyncio.create_task(manager.resume(run_id, "yes"))
    await model.resumed.wait()

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    model.release.set()
    for _ in range(100):
        if (await agent.get_run(run_id)).status is RunStatus.COMPLETED:
            break
        await asyncio.sleep(0)

    assert (await agent.get_run(run_id)).status is RunStatus.COMPLETED
    assert model.calls == 2


@tool
async def make_report(context: ToolContext) -> dict[str, str]:
    """Create a report Artifact."""
    artifact = await context.artifacts.create(
        name="report 2026.txt",
        media_type="text/plain",
        content=b"server artifact",
    )
    return {"artifact_id": str(artifact.id)}


@pytest.mark.asyncio
async def test_server_lists_and_downloads_run_artifacts() -> None:
    agent = Agent(
        profile=AgentProfile(
            id="artifact-server",
            instructions="Create report.",
            tools=("make_report",),
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall(id="call-1", name="make_report", arguments={}),)
                ),
                ModelResponse(content="done"),
            ]
        ),
        tools=(make_report,),
    )

    async with server_client(agent) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "create"})
        run_id = started.json()["run_id"]
        await wait_for_status(client, run_id, RunStatus.COMPLETED)
        listed = await client.get(f"/v1/runs/{run_id}/artifacts")
        artifact_id = listed.json()[0]["id"]
        metadata = await client.get(f"/v1/runs/{run_id}/artifacts/{artifact_id}")
        content = await client.get(
            f"/v1/runs/{run_id}/artifacts/{artifact_id}/content"
        )
        foreign = await client.get(
            f"/v1/runs/{uuid4()}/artifacts/{artifact_id}"
        )

        assert listed.status_code == metadata.status_code == content.status_code == 200
        assert metadata.json()["name"] == "report 2026.txt"
        assert content.content == b"server artifact"
        assert content.headers["x-content-type-options"] == "nosniff"
        assert "report%202026.txt" in content.headers["content-disposition"]
        assert foreign.status_code == 404


@pytest.mark.asyncio
async def test_server_resolves_attachment_ids_without_accepting_inline_bytes() -> None:
    model = FakeModel([ModelResponse(content="seen")])
    agent = Agent(
        profile=AgentProfile(id="attachment-server", instructions="Inspect."),
        model=model,
    )
    attachment = await agent.add_attachment(
        name="input.txt", media_type="text/plain", content=b"stored input"
    )

    async with server_client(agent) as (client, _):
        started = await client.post(
            "/v1/runs",
            json={"prompt": "inspect", "attachment_ids": [str(attachment.id)]},
        )
        run_id = started.json()["run_id"]
        await wait_for_status(client, run_id, RunStatus.COMPLETED)

        assert model.requests[0].attachments == (attachment,)
        assert b"stored input" not in started.content


class HistoryOnlyEventStore:
    def __init__(self) -> None:
        self.delegate = InMemoryEventStore()

    async def emit(
        self,
        run_id: UUID,
        event_type: EventType,
        data: dict[str, Any] | None = None,
    ) -> RuntimeEvent:
        return await self.delegate.emit(run_id, event_type, data)

    async def list(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        return await self.delegate.list(run_id)


@pytest.mark.asyncio
async def test_server_reports_when_event_store_cannot_stream() -> None:
    agent = Agent(
        profile=AgentProfile(id="history-only", instructions="Work."),
        model=FakeModel([ModelResponse(content="done")]),
        event_store=HistoryOnlyEventStore(),
    )

    async with server_client(agent) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "work"})
        run_id = started.json()["run_id"]
        await wait_for_status(client, run_id, RunStatus.COMPLETED)
        response = await client.get(f"/v1/runs/{run_id}/events/stream")

        assert response.status_code == 501


def test_server_rejects_ambiguous_route_prefixes() -> None:
    agent = Agent(
        profile=AgentProfile(id="prefix", instructions="Work."),
        model=FakeModel([]),
    )

    with pytest.raises(ValueError, match="prefix"):
        create_app(agent, prefix="v1")
    with pytest.raises(ValueError, match="prefix"):
        create_app(agent, prefix="/v1/")


def test_artifact_content_route_is_disabled_by_default() -> None:
    agent = Agent(
        profile=AgentProfile(id="safe-content", instructions="Work."),
        model=FakeModel([]),
    )
    paths = create_app(agent).openapi()["paths"]

    assert not any(path.endswith("/content") for path in paths)
