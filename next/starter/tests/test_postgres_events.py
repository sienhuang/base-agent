from __future__ import annotations

import asyncio
import os
from typing import Any
from uuid import uuid4

import httpx
import pytest
from base_agent.events.postgres import PostgresEventStore, PostgresRunStore
from base_agent.models import RunStatus
from base_agent.server.worker import ExecutionWorker, default_python_child_command
from sqlalchemy.schema import DropSchema

from agent_app import asgi
from agent_app.config import Settings

POSTGRES_DSN = os.getenv("BASE_AGENT_NEXT_TEST_POSTGRES_DSN")
requires_postgres = pytest.mark.skipif(
    POSTGRES_DSN is None,
    reason="set BASE_AGENT_NEXT_TEST_POSTGRES_DSN to run PostgreSQL integration tests",
)


@requires_postgres
async def test_queued_fastapi_and_worker_execute_in_an_isolated_child(
    monkeypatch,
    tmp_path,
) -> None:
    assert POSTGRES_DSN is not None
    schema = f"starter_worker_{uuid4().hex}"
    artifact_root = tmp_path / "artifacts"
    settings = Settings(
        provider="offline",
        artifact_root=artifact_root,
        event_store="postgres",
        postgres_dsn=POSTGRES_DSN,
        postgres_schema=schema,
        event_poll_interval_seconds=0.01,
        execution_mode="postgres-worker",
        runtime_route="starter-test",
        runtime_build_id="starter-worker-test",
        operator_token="api-only-test-token",
    )
    child_environment = {
        "AGENT_PROVIDER": "offline",
        "AGENT_EVENT_STORE": "postgres",
        "AGENT_POSTGRES_DSN": POSTGRES_DSN,
        "AGENT_POSTGRES_SCHEMA": schema,
        "AGENT_EVENT_POLL_INTERVAL_SECONDS": "0.01",
        "AGENT_EXECUTION_MODE": "postgres-worker",
        "AGENT_RUNTIME_ROUTE": "starter-test",
        "AGENT_RUNTIME_BUILD_ID": "starter-worker-test",
        "AGENT_ARTIFACT_ROOT": str(artifact_root),
    }
    for name, value in child_environment.items():
        monkeypatch.setenv(name, value)
    application = asgi.create_app(settings)
    worker = ExecutionWorker(
        application.state.execution_store,
        child_command=default_python_child_command("agent_app.execution_child"),
        runtime_route=settings.runtime_route,
        runtime_build_id="starter-worker-test",
        lease_seconds=2,
        heartbeat_interval_seconds=0.2,
        poll_interval_seconds=0.01,
        reconcile_interval_seconds=0.05,
        child_env_remove=("AGENT_OPERATOR_TOKEN",),
        child_env_overrides={
            "AGENT_OPERATOR_TOKEN": "execution-child-no-operator-authority"
        },
    )

    try:
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client:
                started = await client.post(
                    "/v1/runs",
                    headers={"Idempotency-Key": "isolated-child-1"},
                    json={"prompt": "isolated worker"},
                )
                started.raise_for_status()
                run_id = started.json()["run_id"]
                assert started.json()["status"] == "queued"

                assert await worker.run_once() is True
                completed = await client.get(f"/v1/runs/{run_id}")
                history = await client.get(f"/v1/runs/{run_id}/events")

        completed.raise_for_status()
        history.raise_for_status()
        kinds = [event["kind"] for event in history.json()]
        assert completed.json()["status"] == "succeeded"
        assert kinds.count("run.succeeded") == 1
        assert "execution.child.succeeded" in kinds
        assert all(
            event["payload"].get("execution_attempt_id")
            for event in history.json()
            if event["kind"].startswith("execution.child.")
        )
    finally:
        reopened = PostgresEventStore.from_url(POSTGRES_DSN, schema=schema)
        try:
            async with reopened.engine.begin() as connection:
                await connection.execute(
                    DropSchema(schema, cascade=True, if_exists=True)
                )
        finally:
            await reopened.close()


async def _wait_for_terminal(
    client: httpx.AsyncClient,
    path: str,
) -> dict[str, Any]:
    for _ in range(200):
        response = await client.get(path)
        response.raise_for_status()
        payload = response.json()
        if payload["status"] != "running":
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path}")


@requires_postgres
async def test_fastapi_persists_agent_flow_and_child_events_across_store_reopen(
    tmp_path,
) -> None:
    assert POSTGRES_DSN is not None
    schema = f"starter_events_{uuid4().hex}"
    settings = Settings(
        provider="offline",
        artifact_root=tmp_path / "artifacts",
        event_store="postgres",
        postgres_dsn=POSTGRES_DSN,
        postgres_schema=schema,
        event_poll_interval_seconds=0.01,
    )
    application = asgi.create_app(settings)

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            started_run = await client.post(
                "/v1/runs",
                json={"prompt": "persist this Agent Run"},
            )
            started_run.raise_for_status()
            run_id = started_run.json()["run_id"]
            run = await _wait_for_terminal(client, f"/v1/runs/{run_id}")

            started_flow = await client.post(
                "/v1/flows/plan-react",
                json={"prompt": "calculate 4 * 30"},
            )
            started_flow.raise_for_status()
            flow_run_id = started_flow.json()["flow_run_id"]
            flow = await _wait_for_terminal(client, f"/v1/flows/{flow_run_id}")

            run_history = await client.get(f"/v1/runs/{run_id}/events")
            flow_history = await client.get(f"/v1/flows/{flow_run_id}/events")
            run_history.raise_for_status()
            flow_history.raise_for_status()

    assert run["status"] == "succeeded"
    assert flow["status"] == "succeeded"
    assert run_history.json()[-1]["kind"] == "run.succeeded"
    assert flow_history.json()[-1]["kind"] == "flow.succeeded"

    restarted_application = asgi.create_app(settings)
    async with restarted_application.router.lifespan_context(restarted_application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=restarted_application),
            base_url="http://testserver",
        ) as restarted_client:
            recovered_run = await restarted_client.get(f"/v1/runs/{run_id}")
            recovered_flow = await restarted_client.get(
                f"/v1/flows/{flow_run_id}"
            )
            recovered_run_history = await restarted_client.get(
                f"/v1/runs/{run_id}/events"
            )
            recovered_flow_history = await restarted_client.get(
                f"/v1/flows/{flow_run_id}/events"
            )
            recovered_run_stream = await restarted_client.get(
                f"/v1/runs/{run_id}/events/stream"
            )
            recovered_flow_stream = await restarted_client.get(
                f"/v1/flows/{flow_run_id}/events/stream"
            )
            run_as_flow = await restarted_client.get(f"/v1/flows/{run_id}")
            flow_as_run = await restarted_client.get(f"/v1/runs/{flow_run_id}")

    assert recovered_run.status_code == recovered_flow.status_code == 200
    assert recovered_run.json() == run
    assert recovered_flow.json() == flow
    assert recovered_run_history.json() == run_history.json()
    assert recovered_flow_history.json() == flow_history.json()
    assert "event: run.succeeded" in recovered_run_stream.text
    assert "event: flow.succeeded" in recovered_flow_stream.text
    assert run_as_flow.status_code == flow_as_run.status_code == 404

    reopened = PostgresEventStore.from_url(
        POSTGRES_DSN,
        schema=schema,
        poll_interval=0.01,
    )
    reopened_runs = PostgresRunStore.from_event_store(reopened)
    try:
        replayed_run = await reopened.list(run_id)
        replayed_flow = await reopened.list(flow_run_id)
        run_record = await reopened_runs.get(run_id)

        assert replayed_run[-1].kind == "run.succeeded"
        assert run_record.status is RunStatus.SUCCEEDED
        assert run_record.output == run["output"]
        assert replayed_flow[-1].kind == "flow.succeeded"
        assert len(replayed_run) == len(run_history.json())
        assert len(replayed_flow) == len(flow_history.json())
        assert any(
            event.payload.get("event_scope") == "child_run"
            for event in replayed_flow
        )

        child_run_ids = [child["agent_run_id"] for child in flow["child_runs"]]
        assert len(child_run_ids) == 3
        for child_run_id in child_run_ids:
            child_events = await reopened.list(child_run_id)
            assert child_events[0].kind == "run.started"
            assert child_events[-1].kind == "run.succeeded"
    finally:
        async with reopened.engine.begin() as connection:
            await connection.execute(
                DropSchema(schema, cascade=True, if_exists=True)
            )
        await reopened.close()
