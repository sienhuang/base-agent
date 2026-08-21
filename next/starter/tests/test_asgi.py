import asyncio

import httpx
from base_agent.events import InMemoryEventStore

from agent_app import asgi
from agent_app.app import STARTER_AGENT_DEFINITION
from agent_app.config import Settings


def test_queued_api_requires_operator_credentials() -> None:
    settings = Settings(
        provider="offline",
        event_store="postgres",
        postgres_dsn="postgresql://agent:password@db/agent",
        execution_mode="postgres-worker",
    )

    try:
        asgi.create_app(settings)
    except ValueError as exc:
        assert str(exc) == (
            "AGENT_OPERATOR_TOKEN is required by the API in postgres-worker mode"
        )
    else:
        raise AssertionError("queued API without Operator credentials must fail")


def test_starter_composes_sdk_http_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        asgi.Settings,
        "from_env",
        lambda: Settings(provider="offline"),
    )
    built = asgi.create_app()

    assert asgi.app.title == "base-agent Starter"
    assert built.title == "base-agent Starter"
    assert {route.path for route in built.routes} >= {
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/cancel",
        "/v1/runs/{run_id}/resume",
        "/v1/runs/{run_id}/confirm",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/events/stream",
        "/v1/flows/plan-react",
        "/v1/flows/{flow_run_id}",
        "/v1/flows/{flow_run_id}/cancel",
        "/v1/flows/{flow_run_id}/resume",
        "/v1/flows/{flow_run_id}/confirm",
        "/v1/flows/{flow_run_id}/events",
        "/v1/flows/{flow_run_id}/events/stream",
    }
    assert callable(built.state.run_registry.get)
    assert built.state.flow_registry.names == ("plan-react",)
    assert isinstance(built.state.event_store, InMemoryEventStore)
    openapi_paths = built.openapi()["paths"]
    assert "post" in openapi_paths["/v1/flows/plan-react"]
    assert "get" in openapi_paths["/v1/flows/{flow_run_id}"]


async def test_http_uses_the_shared_starter_definition(monkeypatch) -> None:
    monkeypatch.setattr(
        asgi.Settings,
        "from_env",
        lambda: Settings(provider="offline"),
    )
    built = asgi.create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built),
        base_url="http://testserver",
    ) as client:
        started = await client.post(
            "/v1/runs",
            json={
                "prompt": (
                    'tool=calculate_product calls=1 '
                    'tool_args={"left":4,"right":30}'
                )
            },
        )
        run_id = started.json()["run_id"]
        run_result = await built.state.run_registry.get(run_id).wait()
        result = await client.get(f"/v1/runs/{run_id}")
        events = await client.get(f"/v1/runs/{run_id}/events")

    assert started.status_code == 202
    assert result.json()["status"] == "succeeded"
    payloads = events.json()
    skill_events = [event for event in payloads if event["kind"].startswith("skill.")]
    assert [event["kind"] for event in skill_events] == [
        "skill.selected",
        "skill.selected",
        "skill.resource.used",
    ]
    assert [event["payload"]["skill_name"] for event in skill_events] == [
        "python-calculator",
        "value-recording",
        "python-calculator",
    ]
    completed = next(event for event in payloads if event["kind"] == "tool.completed")
    assert completed["turn"] == 1
    assert "turn" not in completed["payload"]
    assert completed["payload"]["tool_name"] == "calculate_product"
    assert completed["payload"]["status"] == "success"
    assert run_result.tool_results[0].data == {"product": 120}
    assert run_result.snapshot.skill_names == STARTER_AGENT_DEFINITION.skills
    assert run_result.snapshot.permissions == STARTER_AGENT_DEFINITION.permissions


async def test_http_waits_for_and_accepts_exact_tool_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        asgi.Settings,
        "from_env",
        lambda: Settings(provider="offline"),
    )
    built = asgi.create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built),
        base_url="http://testserver",
    ) as client:
        started = await client.post(
            "/v1/runs",
            json={
                "prompt": 'tool=save_value calls=1 tool_args={"value":42}',
            },
        )
        run_id = started.json()["run_id"]
        waiting = await built.state.run_registry.get(run_id).wait()
        waiting_response = await client.get(f"/v1/runs/{run_id}")
        assert waiting.pending_input is not None
        confirmed = await client.post(
            f"/v1/runs/{run_id}/confirm",
            json={
                "request_id": waiting.pending_input.request_id,
                "decision": "approve",
                "subject_id": "starter-test-user",
                "reason_code": "manual_test",
            },
        )

    assert waiting_response.json()["status"] == "waiting"
    assert waiting_response.json()["pending_input"]["kind"] == "tool_confirmation"
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "succeeded"
    assert "executed=1" in confirmed.json()["text"]


async def test_http_resumes_after_the_model_calls_ask_user(monkeypatch) -> None:
    monkeypatch.setattr(
        asgi.Settings,
        "from_env",
        lambda: Settings(provider="offline"),
    )
    built = asgi.create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built),
        base_url="http://testserver",
    ) as client:
        started = await client.post(
            "/v1/runs",
            json={
                "prompt": (
                    'tool=ask_user calls=1 tool_args={"question":"Which region?",'
                    '"metadata":{"field":"region"}}'
                ),
            },
        )
        run_id = started.json()["run_id"]
        waiting = await built.state.run_registry.get(run_id).wait()
        waiting_response = await client.get(f"/v1/runs/{run_id}")
        resumed = await client.post(
            f"/v1/runs/{run_id}/resume",
            json={"input": "APAC"},
        )

    assert waiting.status.value == "waiting"
    assert waiting_response.json()["pending_input"] == {
        "request_id": waiting.pending_input.request_id,
        "kind": "tool_input",
        "prompt": "Which region?",
        "tool_name": "ask_user",
        "tool_call_id": "call-1",
        "metadata": {"field": "region"},
    }
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"
    assert "executed=1" in resumed.json()["text"]


async def test_http_resumes_plan_react_after_executor_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        asgi.Settings,
        "from_env",
        lambda: Settings(provider="offline"),
    )
    built = asgi.create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built),
        base_url="http://testserver",
    ) as client:
        started = await client.post(
            "/v1/flows/plan-react",
            json={
                "prompt": 'tool=save_value calls=1 tool_args={"value":42}',
            },
        )
        flow_run_id = started.json()["flow_run_id"]
        waiting = await built.state.flow_registry.get(flow_run_id).wait()
        assert waiting.pending_input is not None
        confirmed = await client.post(
            f"/v1/flows/{flow_run_id}/confirm",
            json={
                "request_id": waiting.pending_input.request_id,
                "decision": "approve",
                "subject_id": "starter-test-user",
                "reason_code": "manual_test",
            },
        )

    assert confirmed.status_code == 200
    payload = confirmed.json()
    assert payload["status"] == "succeeded"
    assert [child["status"] for child in payload["child_runs"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]


async def test_http_plan_react_uses_starter_skills_and_streams_child_events(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        asgi.Settings,
        "from_env",
        lambda: Settings(provider="offline"),
    )
    built = asgi.create_app()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=built),
        base_url="http://testserver",
    ) as client:
        started = await client.post(
            "/v1/flows/plan-react",
            json={
                "prompt": (
                    'tool=calculate_product calls=1 '
                    'tool_args={"left":4,"right":30}'
                ),
                "stream": True,
            },
        )
        flow_run_id = started.json()["flow_run_id"]
        await asyncio.wait_for(
            built.state.flow_registry.get(flow_run_id).wait(),
            timeout=2,
        )
        response = await client.get(f"/v1/flows/{flow_run_id}")
        events = await client.get(f"/v1/flows/{flow_run_id}/events")
        stream = await client.get(f"/v1/flows/{flow_run_id}/events/stream")

    assert started.status_code == 202
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "succeeded"
    assert payload["plan"]["status"] == "succeeded"
    assert '"product":120' in payload["output"]
    assert events.json()[0]["payload"]["stream"] is True
    assert [child["role"] for child in payload["child_runs"]] == [
        "planner",
        "executor",
        "planner",
    ]

    child_events = [
        event
        for event in events.json()
        if event["payload"].get("event_scope") == "child_run"
    ]
    tool_completed = next(
        event
        for event in child_events
        if event["kind"] == "tool.completed"
    )
    assert tool_completed["payload"]["child_role"] == "executor"
    assert tool_completed["payload"]["tool_name"] == "calculate_product"
    assert tool_completed["payload"]["status"] == "success"
    assert tool_completed["payload"]["agent_run_id"] == payload["child_runs"][1][
        "agent_run_id"
    ]
    output_completed = next(
        event
        for event in events.json()
        if event["kind"] == "message.completed"
        and event["payload"].get("event_scope") == "flow"
    )
    assert '"product":120' in output_completed["payload"]["content"]
    stream_text = stream.text.replace("\r\n", "\n")
    assert "event: tool.completed" in stream_text
    assert '"event_scope":"child_run"' in stream_text
    assert "event: child.message.delta" in stream_text
    assert "event: model.tool_call.delta" not in stream_text
    assert "event: child.model.tool_call.delta" not in stream_text
    assert "event: message.completed" in stream_text
    assert "event: flow.succeeded" in stream_text
