import asyncio

import httpx

from agent_app import asgi
from agent_app.app import STARTER_AGENT_DEFINITION
from agent_app.config import Settings


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
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/events/stream",
    }
    assert callable(built.state.run_registry.get)


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
        for _ in range(100):
            result = await client.get(f"/v1/runs/{run_id}")
            if result.json()["status"] != "running":
                break
            await asyncio.sleep(0)
        events = await client.get(f"/v1/runs/{run_id}/events")
        run_result = await built.state.run_registry.get(run_id).wait()

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
