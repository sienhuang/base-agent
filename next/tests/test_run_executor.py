from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import BaseModel

from base_agent import Agent, AgentDefinition, WaitForInput, tool
from base_agent.checkpoints import CheckpointNotFoundError, InMemoryCheckpointStore
from base_agent.events import InMemoryEventStore
from base_agent.models import ModelRequest, ModelResponse, RunStatus, ToolCall
from base_agent.providers import ProviderCapabilities, ProviderInfo
from base_agent.run_store import AgentRunRecord, InMemoryRunStore
from base_agent.server import LocalRunExecutor, create_http_app
from base_agent.testing import FakeModel


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 20, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class GatedModel:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="gated",
            default_model="gated",
            capabilities=ProviderCapabilities(),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requested.set()
        await self.release.wait()
        return ModelResponse(content="late answer")


class StructuredAnswer(BaseModel):
    value: int


def _definition(*tools: str) -> AgentDefinition:
    return AgentDefinition(
        id="durable-agent",
        version="1.0.0",
        instructions="Answer or ask for required input.",
        tools=tools,
        max_model_turns=3,
    )


async def _wait_for_status(
    client: httpx.AsyncClient,
    run_id: str,
    expected: RunStatus,
) -> dict[str, object]:
    for _ in range(100):
        response = await client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == expected.value:
            return payload
        await asyncio.sleep(0)
    raise AssertionError(f"Run did not reach {expected.value}")


async def test_shared_run_store_recovers_completed_status_after_http_restart() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    first_agent = Agent(
        _definition(),
        model=FakeModel.text("durable answer"),
        event_store=events,
    )
    first_app = create_http_app(first_agent, event_store=events, run_store=runs)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app),
        base_url="http://test",
    ) as client:
        started = await client.post("/v1/runs", json={"prompt": "answer"})
        run_id = started.json()["run_id"]
        completed = await _wait_for_status(client, run_id, RunStatus.SUCCEEDED)

    recreated_agent = Agent(
        _definition(),
        model=FakeModel.text("must not be called"),
        event_store=events,
    )
    restarted_app = create_http_app(
        recreated_agent,
        event_store=events,
        run_store=runs,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app),
        base_url="http://test",
    ) as client:
        recovered = await client.get(f"/v1/runs/{run_id}")

    assert recovered.status_code == 200
    assert recovered.json() == completed
    assert recreated_agent.snapshot.fingerprint == first_agent.snapshot.fingerprint


async def test_structured_run_output_is_persisted_as_json() -> None:
    events = InMemoryEventStore()
    runs = InMemoryRunStore()
    agent = Agent(
        AgentDefinition(
            id="structured-agent",
            version="1.0.0",
            instructions="Return JSON.",
            response_model=StructuredAnswer,
        ),
        model=FakeModel.text('{"value":42}'),
        event_store=events,
    )
    app = create_http_app(agent, event_store=events, run_store=runs)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        started = await client.post("/v1/runs", json={"prompt": "answer"})
        completed = await _wait_for_status(
            client,
            started.json()["run_id"],
            RunStatus.SUCCEEDED,
        )

    assert completed["output"] == {"value": 42}


async def test_expired_running_record_is_blocked_without_reexecution() -> None:
    clock = MutableClock()
    events = InMemoryEventStore()
    runs = InMemoryRunStore(clock=clock)
    model = FakeModel.text("must not be called")
    agent = Agent(_definition(), model=model, event_store=events)
    snapshot = agent.snapshot
    await runs.create(
        AgentRunRecord(
            run_id="abandoned-run",
            agent_id=snapshot.agent_id,
            agent_version=snapshot.agent_version,
            snapshot_fingerprint=snapshot.fingerprint,
            runtime_build_id="old-build",
            created_at=clock(),
            updated_at=clock(),
        )
    )
    await runs.claim(
        "abandoned-run",
        owner_id="dead-process",
        lease_seconds=10,
    )
    clock.advance(seconds=11)
    app = create_http_app(agent, event_store=events, run_store=runs)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get("/v1/runs/abandoned-run")
        second = await client.get("/v1/runs/abandoned-run")

    assert response.status_code == 200
    assert response.json()["status"] == "blocked"
    assert "automatic replay is disabled" in response.json()["error"]
    assert second.json() == response.json()
    assert model.requests == ()
    assert [event.kind for event in await events.list("abandoned-run")] == ["run.blocked"]


async def test_local_executor_stops_fenced_work_without_rewriting_blocked_event() -> None:
    clock = MutableClock()
    events = InMemoryEventStore()
    runs = InMemoryRunStore(clock=clock)
    model = GatedModel()
    agent = Agent(_definition(), model=model, event_store=events)
    executor = LocalRunExecutor(
        agent,
        runs,
        lease_seconds=0.06,
        heartbeat_interval_seconds=0.02,
    )
    started = await executor.start("wait")
    await model.requested.wait()
    clock.advance(seconds=1)

    blocked = await executor.reconcile_expired()
    await asyncio.sleep(0.03)

    assert [record.run_id for record in blocked] == [started.run_id]
    assert (await runs.get(started.run_id)).status is RunStatus.BLOCKED
    assert [event.kind for event in await events.list(started.run_id)][-1] == "run.blocked"
    assert "run.cancelled" not in {
        event.kind for event in await events.list(started.run_id)
    }
    await executor.close()


async def test_waiting_run_resumes_through_recreated_http_executor() -> None:
    @tool
    async def ask_user() -> WaitForInput:
        return WaitForInput("Which region?", {"field": "region"})

    events = InMemoryEventStore()
    checkpoints = InMemoryCheckpointStore()
    runs = InMemoryRunStore()
    first_agent = Agent(
        _definition("ask_user"),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="ask-region", name="ask_user", arguments={}),
                    )
                )
            ]
        ),
        tools=(ask_user,),
        event_store=events,
        checkpoint_store=checkpoints,
    )
    first_app = create_http_app(first_agent, event_store=events, run_store=runs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=first_app),
        base_url="http://test",
    ) as client:
        started = await client.post("/v1/runs", json={"prompt": "analyze"})
        run_id = started.json()["run_id"]
        waiting = await _wait_for_status(client, run_id, RunStatus.WAITING)

    recreated = Agent(
        _definition("ask_user"),
        model=FakeModel.text("APAC analysis complete"),
        tools=(ask_user,),
        event_store=events,
        checkpoint_store=checkpoints,
    )
    restarted_app = create_http_app(recreated, event_store=events, run_store=runs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app),
        base_url="http://test",
    ) as client:
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"input": "APAC"},
        )

    assert waiting["pending_input"] == {
        "request_id": waiting["pending_input"]["request_id"],
        "kind": "tool_input",
        "tool_call_id": "ask-region",
        "tool_name": "ask_user",
        "prompt": "Which region?",
        "metadata": {"field": "region"},
    }
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"
    assert resumed.json()["text"] == "APAC analysis complete"
    assert (await runs.get(run_id)).checkpoint_revision is None


async def test_waiting_run_can_be_cancelled_after_local_handle_settles() -> None:
    @tool
    async def ask_user() -> WaitForInput:
        return WaitForInput("Continue?")

    events = InMemoryEventStore()
    checkpoints = InMemoryCheckpointStore()
    runs = InMemoryRunStore()
    agent = Agent(
        _definition("ask_user"),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="ask-cancel", name="ask_user", arguments={}),
                    )
                )
            ]
        ),
        tools=(ask_user,),
        event_store=events,
        checkpoint_store=checkpoints,
    )
    app = create_http_app(agent, event_store=events, run_store=runs)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        started = await client.post("/v1/runs", json={"prompt": "ask"})
        run_id = started.json()["run_id"]
        await _wait_for_status(client, run_id, RunStatus.WAITING)
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert (await runs.get(run_id)).status is RunStatus.CANCELLED
    with pytest.raises(CheckpointNotFoundError):
        await checkpoints.load(run_id)
