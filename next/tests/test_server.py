import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from base_agent import Agent, AgentDefinition
from base_agent.events import InMemoryEventStore
from base_agent.models import Event, ModelRequest, ModelResponse
from base_agent.providers import ModelProvider, ProviderCapabilities, ProviderInfo
from base_agent.server import InMemoryRunRegistry, create_http_app
from base_agent.testing import FakeModel


class GatedModel:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="http-gated-model",
            default_model="http-gated-model",
            capabilities=ProviderCapabilities(),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requested.set()
        await self.release.wait()
        return ModelResponse(content="HTTP done")


def _agent(model: ModelProvider) -> Agent:
    return Agent(
        AgentDefinition(
            id="http-assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
    )


@asynccontextmanager
async def _client(
    agent: Agent,
    *,
    sse_ping_seconds: int = 15,
) -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    app = create_http_app(agent, sse_ping_seconds=sse_ping_seconds)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, app


async def _wait_for_status(
    client: httpx.AsyncClient,
    run_id: str,
    expected: str,
) -> dict[str, Any]:
    for _ in range(100):
        response = await client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == expected:
            return payload
        await asyncio.sleep(0)
    raise AssertionError(f"Run did not reach status {expected!r}")


async def test_http_starts_without_waiting_and_returns_completed_result() -> None:
    model = GatedModel()

    async with _client(_agent(model)) as (client, app):
        started = await client.post("/v1/runs", json={"prompt": "hello"})

        assert started.status_code == 202
        assert started.json()["status"] == "running"
        run_id = started.json()["run_id"]
        assert isinstance(app.state.run_registry, InMemoryRunRegistry)
        await asyncio.wait_for(model.requested.wait(), timeout=1)

        running = await client.get(f"/v1/runs/{run_id}")
        assert running.status_code == 200
        assert running.json() == {
            "run_id": run_id,
            "status": "running",
            "event_count": 5,
            "output": None,
            "text": None,
            "error": None,
        }

        model.release.set()
        completed = await _wait_for_status(client, run_id, "succeeded")

    assert completed == {
        "run_id": run_id,
        "status": "succeeded",
        "event_count": 8,
        "output": "HTTP done",
        "text": "HTTP done",
        "error": None,
    }


async def test_http_cancels_running_run_and_replays_terminal_event() -> None:
    model = GatedModel()

    async with _client(_agent(model)) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await asyncio.wait_for(model.requested.wait(), timeout=1)

        cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
        repeated = await client.post(f"/v1/runs/{run_id}/cancel")
        events = await client.get(f"/v1/runs/{run_id}/events")
        stream = await client.get(f"/v1/runs/{run_id}/events/stream")

    expected = {
        "run_id": run_id,
        "status": "cancelled",
        "event_count": 6,
        "output": None,
        "text": "",
        "error": "run cancellation requested",
    }
    assert cancelled.status_code == repeated.status_code == 200
    assert cancelled.json() == repeated.json() == expected
    assert [event["kind"] for event in events.json()][-2:] == [
        "model.requested",
        "run.cancelled",
    ]
    terminal = events.json()[-1]
    assert terminal["turn"] == 1
    assert terminal["payload"] == {"reason": "run cancellation requested"}
    stream_text = stream.text.replace("\r\n", "\n")
    assert "id: 6\nevent: run.cancelled" in stream_text


async def test_http_rejects_cancellation_for_unknown_or_completed_run() -> None:
    async with _client(_agent(FakeModel.text("done"))) as (client, _):
        missing = await client.post("/v1/runs/missing/cancel")
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await _wait_for_status(client, run_id, "succeeded")
        completed = await client.post(f"/v1/runs/{run_id}/cancel")

    assert missing.status_code == 404
    assert completed.status_code == 409
    assert "already reached status 'succeeded'" in completed.json()["detail"]


async def test_http_replays_persisted_events_after_sequence_cursor() -> None:
    async with _client(_agent(FakeModel.text("done"))) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await _wait_for_status(client, run_id, "succeeded")

        response = await client.get(
            f"/v1/runs/{run_id}/events",
            params={"after_sequence": 5},
        )

    assert response.status_code == 200
    assert [event["sequence"] for event in response.json()] == [6, 7, 8]
    assert [event["kind"] for event in response.json()] == [
        "model.responded",
        "message.completed",
        "run.succeeded",
    ]
    assert response.json()[1]["payload"]["content"] == "done"
    assert all(event["run_id"] == run_id for event in response.json())
    assert [event["turn"] for event in response.json()] == [1, 1, 1]
    assert all("turn" not in event["payload"] for event in response.json())
    assert all(isinstance(event["payload"], dict) for event in response.json())


async def test_sse_replays_and_resumes_with_query_and_last_event_id_cursors() -> None:
    async with _client(_agent(FakeModel.text("done"))) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await _wait_for_status(client, run_id, "succeeded")

        replay = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            params={"after_sequence": 5},
        )
        header_resume = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "6"},
        )
        query_resume = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            params={"last_event_id": "6"},
        )
        larger_cursor = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            params={"after_sequence": 6},
            headers={"Last-Event-ID": "5"},
        )
        exhausted = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "8"},
        )

    replay_text = replay.text.replace("\r\n", "\n")
    header_text = header_resume.text.replace("\r\n", "\n")
    query_text = query_resume.text.replace("\r\n", "\n")
    larger_text = larger_cursor.text.replace("\r\n", "\n")
    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("text/event-stream")
    assert replay.headers["x-accel-buffering"] == "no"
    assert "id: 6\nevent: model.responded" in replay_text
    assert "id: 7\nevent: message.completed" in replay_text
    assert '"content":"done"' in replay_text
    assert "id: 8\nevent: run.succeeded" in replay_text
    assert '"sequence":6' in replay_text
    assert '"turn":1' in replay_text
    assert "id: 6" not in header_text
    assert "id: 7\nevent: message.completed" in header_text
    assert "id: 8\nevent: run.succeeded" in header_text
    assert query_text == header_text == larger_text
    assert exhausted.text == ""


async def test_sse_follows_live_events_and_sends_periodic_ping() -> None:
    model = GatedModel()

    async with _client(_agent(model), sse_ping_seconds=1) as (client, app):
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await asyncio.wait_for(model.requested.wait(), timeout=1)

        async def release_after_ping() -> None:
            await asyncio.sleep(1.1)
            model.release.set()

        release = asyncio.create_task(release_after_ping())
        response = await client.get(f"/v1/runs/{run_id}/events/stream")
        await release

    response_text = response.text.replace("\r\n", "\n")
    assert app.state.sse_ping_seconds == 1
    assert response.status_code == 200
    assert ": ping - " in response_text
    assert '"turn":null' in response_text
    assert "id: 1\nevent: run.started" in response_text
    assert "id: 2\nevent: message.created" in response_text
    assert "id: 7\nevent: message.completed" in response_text
    assert "id: 8\nevent: run.succeeded" in response_text


async def test_event_routes_validate_run_and_cursors() -> None:
    async with _client(_agent(FakeModel.text("done"))) as (client, _):
        missing_history = await client.get("/v1/runs/missing/events")
        missing_stream = await client.get("/v1/runs/missing/events/stream")
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await _wait_for_status(client, run_id, "succeeded")
        negative_query = await client.get(
            f"/v1/runs/{run_id}/events",
            params={"after_sequence": -1},
        )
        invalid_header = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        negative_header = await client.get(
            f"/v1/runs/{run_id}/events/stream",
            headers={"Last-Event-ID": "-1"},
        )

    assert missing_history.status_code == missing_stream.status_code == 404
    assert negative_query.status_code == 422
    assert invalid_header.status_code == 400
    assert invalid_header.json() == {"detail": "Last-Event-ID must be an integer"}
    assert negative_header.status_code == 400
    assert negative_header.json() == {"detail": "Last-Event-ID must be non-negative"}


async def test_http_returns_404_for_unknown_run() -> None:
    async with _client(_agent(GatedModel())) as (client, _):
        response = await client.get("/v1/runs/missing")

    assert response.status_code == 404
    assert response.json() == {"detail": "Run 'missing' was not found"}


async def test_http_rejects_blank_prompt_and_unknown_fields() -> None:
    async with _client(_agent(GatedModel())) as (client, _):
        blank = await client.post("/v1/runs", json={"prompt": " "})
        extra = await client.post(
            "/v1/runs",
            json={"prompt": "hello", "permissions": ["admin"]},
        )

    assert blank.status_code == 422
    assert extra.status_code == 422


async def test_http_respects_custom_and_root_prefixes() -> None:
    custom_app = create_http_app(_agent(FakeModel.text("done")), prefix="/api")
    root_app = create_http_app(_agent(FakeModel.text("done")), prefix="/")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=custom_app),
        base_url="http://testserver",
    ) as custom_client:
        custom = await custom_client.post("/api/runs", json={"prompt": "hello"})
        old_prefix = await custom_client.post("/v1/runs", json={"prompt": "hello"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=root_app),
        base_url="http://testserver",
    ) as root_client:
        root = await root_client.post("/runs", json={"prompt": "hello"})

    assert custom.status_code == 202
    assert old_prefix.status_code == 404
    assert root.status_code == 202
    await custom_app.state.run_registry.get(custom.json()["run_id"]).wait()
    await root_app.state.run_registry.get(root.json()["run_id"]).wait()


def test_http_rejects_invalid_prefixes() -> None:
    agent = _agent(GatedModel())

    with pytest.raises(ValueError, match="prefix must start"):
        create_http_app(agent, prefix="v1")
    with pytest.raises(ValueError, match="must not end"):
        create_http_app(agent, prefix="/v1/")
    with pytest.raises(ValueError, match="sse_ping_seconds must be at least 1"):
        create_http_app(agent, sse_ping_seconds=0)


async def test_http_reports_when_event_store_cannot_stream() -> None:
    class HistoryOnlyEventStore:
        def __init__(self) -> None:
            self.delegate = InMemoryEventStore()

        async def emit(
            self,
            run_id: str,
            kind: str,
            payload: Mapping[str, Any] | None = None,
            *,
            turn: int | None = None,
        ) -> Event:
            return await self.delegate.emit(run_id, kind, payload, turn=turn)

        async def list(
            self,
            run_id: str,
            *,
            after_sequence: int = 0,
        ) -> tuple[Event, ...]:
            return await self.delegate.list(run_id, after_sequence=after_sequence)

    agent = Agent(
        AgentDefinition(
            id="history-only-http",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("done"),
        event_store=HistoryOnlyEventStore(),
    )

    async with _client(agent) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        run_id = started.json()["run_id"]
        await _wait_for_status(client, run_id, "succeeded")
        history = await client.get(f"/v1/runs/{run_id}/events")
        stream = await client.get(f"/v1/runs/{run_id}/events/stream")

    assert history.status_code == 200
    assert len(history.json()) == 8
    assert stream.status_code == 501
    assert stream.json() == {
        "detail": "the configured EventStore does not support live subscriptions"
    }


async def test_http_reports_unsupported_provider_streaming_as_failed_run() -> None:
    async with _client(_agent(GatedModel())) as (client, _):
        started = await client.post(
            "/v1/runs",
            json={"prompt": "hello", "stream": True},
        )
        failed = await _wait_for_status(client, started.json()["run_id"], "failed")

    assert failed["event_count"] == 0
    assert failed["error"] == (
        "ValueError: Provider 'http-gated-model' does not support streaming"
    )


async def test_http_keeps_concurrent_run_events_isolated() -> None:
    model = GatedModel()
    agent = _agent(model)

    async with _client(agent) as (client, _):
        first = await client.post("/v1/runs", json={"prompt": "first"})
        second = await client.post("/v1/runs", json={"prompt": "second"})
        first_run_id = first.json()["run_id"]
        second_run_id = second.json()["run_id"]

        assert first_run_id != second_run_id
        await asyncio.wait_for(model.requested.wait(), timeout=1)
        model.release.set()
        first_result = await _wait_for_status(client, first_run_id, "succeeded")
        second_result = await _wait_for_status(client, second_run_id, "succeeded")

    first_events = await agent.event_store.list(first_run_id)
    second_events = await agent.event_store.list(second_run_id)
    assert first_result["event_count"] == second_result["event_count"] == 8
    assert {event.run_id for event in first_events} == {first_run_id}
    assert {event.run_id for event in second_events} == {second_run_id}
    assert [event.sequence for event in first_events] == list(range(1, 9))
    assert [event.sequence for event in second_events] == list(range(1, 9))


async def test_http_exposes_harness_failure_as_terminal_run() -> None:
    class FailingModel(GatedModel):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise RuntimeError("provider unavailable")

    async with _client(_agent(FailingModel())) as (client, _):
        started = await client.post("/v1/runs", json={"prompt": "hello"})
        failed = await _wait_for_status(client, started.json()["run_id"], "failed")

    assert failed["error"] == "model request failed: provider unavailable"
    assert failed["output"] is None
    assert failed["event_count"] == 7
