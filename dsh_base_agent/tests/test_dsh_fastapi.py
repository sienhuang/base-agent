from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import httpx
import pytest

from dsh_base_agent import (
    Agent,
    AgentDefinition,
    BaseAgentApp,
    CompactingContextPolicy,
    Flow,
    LocalState,
    RunStatus,
    Stage,
)
from dsh_base_agent._dsh import (
    GeneratedSdkProfilePatches,
    RuntimeSpec,
    SdkRuntimeConfig,
    SdkRuntimeFactory,
)
from dsh_base_agent._dsh import runtime as dsh_runtime_module
from dsh_base_agent.server import (
    DshAgentConfigError,
    DshFastApiSettings,
    create_http_app,
    load_dsh_agent_registry,
)
from dsh_base_agent.server.asgi import create_dsh_app
from dsh_base_agent.testing import ScriptedRuntimeFactory


def _agent(app: BaseAgentApp, name: str) -> Agent:
    return Agent(
        AgentDefinition(
            id=name,
            version="1.0.0",
            instructions=f"Act as {name}.",
        ),
        app=app,
    )


async def _wait_for_run(client: httpx.AsyncClient, run_id: str) -> httpx.Response:
    for _ in range(100):
        response = await client.get(f"/v1/runs/{run_id}")
        if response.json()["status"] != RunStatus.RUNNING:
            return response
        await asyncio.sleep(0.01)
    raise AssertionError(f"Run {run_id} did not reach a terminal status")


async def _wait_for_flow(client: httpx.AsyncClient, flow_run_id: str) -> httpx.Response:
    for _ in range(100):
        response = await client.get(f"/v1/flows/{flow_run_id}")
        if response.json()["status"] != "running":
            return response
        await asyncio.sleep(0.01)
    raise AssertionError(f"Flow {flow_run_id} did not reach a terminal status")


async def test_local_http_control_plane_serves_a_dsh_backed_run(tmp_path) -> None:
    runtime = ScriptedRuntimeFactory({"assistant": ["hello from DSH"]})
    base_app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=runtime,
    )
    agent = _agent(base_app, "assistant")
    application = create_http_app(base_app, agent)

    async with application.router.lifespan_context(application):
        assert runtime.started_agents == ["assistant"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            ready = await client.get("/health/ready")
            started = await client.post("/v1/runs", json={"prompt": "hello"})
            completed = await _wait_for_run(client, started.json()["run_id"])
            events = await client.get(f"/v1/runs/{started.json()['run_id']}/events")
            stream = await client.get(f"/v1/runs/{started.json()['run_id']}/events/stream")

    assert ready.json() == {"status": "ok", "dsh": "ready"}
    assert started.status_code == 202
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["output"] == "hello from DSH"
    assert events.json()[-1]["kind"] == "run.succeeded"
    assert "event: run.succeeded" in stream.text


async def test_local_http_control_plane_exposes_host_owned_flow(tmp_path) -> None:
    runtime = ScriptedRuntimeFactory(
        {"assistant": [], "analyze": ["analysis"], "review": ["approved"]}
    )
    base_app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=runtime,
    )
    assistant = _agent(base_app, "assistant")
    analyze = _agent(base_app, "analyze")
    review = _agent(base_app, "review")
    flow = Flow.sequence(
        id="review-flow",
        stages=(
            Stage(name="analyze", agent=analyze),
            Stage(name="review", agent=review, input_from="analyze"),
        ),
        event_store=base_app.event_store,
    )
    application = create_http_app(
        base_app,
        assistant,
        flows={"review": flow},
        prepare_agents=(analyze, review),
    )

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            started = await client.post(
                "/v1/flows/review",
                json={"prompt": "review this"},
            )
            completed = await _wait_for_flow(client, started.json()["flow_run_id"])
            events = await client.get(
                f"/v1/flows/{started.json()['flow_run_id']}/events"
            )
            stream = await client.get(
                f"/v1/flows/{started.json()['flow_run_id']}/events/stream"
            )

    assert started.status_code == 202
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["output"] == "approved"
    assert events.json()[-1]["kind"] == "flow.succeeded"
    assert "event: flow.succeeded" in stream.text
    assert [call.agent_name for call in runtime.calls] == ["analyze", "review"]


async def test_generated_full_sdk_patch_contains_persona_and_compaction(tmp_path) -> None:
    runtime = ScriptedRuntimeFactory({})
    base_app = BaseAgentApp(state=LocalState(tmp_path), runtime_factory=runtime)
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer only from verified facts.",
        ),
        app=base_app,
        context_policy=CompactingContextPolicy(
            threshold_ratio=0.7,
            retain_ratio=0.1,
        ),
    )

    (patch_path,) = GeneratedSdkProfilePatches()(
        agent.runtime_spec,
        base_app.state.runtime_home(agent.fingerprint),
    )
    patch = json.loads(patch_path.read_text(encoding="utf-8"))

    persona = next(item for item in patch if item.get("id") == "system-prompt")
    compaction = next(item for item in patch if item.get("id") == "compaction-basic")
    assert "Answer only from verified facts." in persona["config"]["persona"]
    assert compaction["config"] == {"retainRatio": 0.1, "thresholdRatio": 0.7}
    assert any(item.get("id") == "tool-bash" and item["disabled"] for item in patch)
    await base_app.close()


def test_real_dsh_asgi_factory_uses_local_run_routes(tmp_path) -> None:
    settings = DshFastApiSettings(
        state_dir=tmp_path / "state",
        workspace=tmp_path,
        agent_name="service-agent",
        instructions="Serve through DSH.",
    )

    application = create_dsh_app(settings)

    assert application.state.default_agent_name == "service-agent"
    assert {route.path for route in application.routes} >= {
        "/health/live",
        "/health/ready",
        "/v1/runs",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/events",
        "/v1/runs/{run_id}/events/stream",
    }


def test_real_dsh_asgi_factory_loads_multi_agent_registry(tmp_path) -> None:
    config_path = tmp_path / "agents.yaml"
    config_path.write_text(
        """\
default_agent: assistant
agents:
  assistant:
    instructions: Answer general questions.
    provider: deepseek-official
    model: deepseek-v4-flash
  planner:
    instructions: Produce a plan.
    provider: deepseek-official
    model: deepseek-v4-flash
  coder:
    instructions: Implement the plan.
    provider: deepseek-official
    model: deepseek-v4-flash
""",
        encoding="utf-8",
    )
    settings = DshFastApiSettings(
        state_dir=tmp_path / "state",
        workspace=tmp_path,
        agent_config_path=config_path,
    )

    application = create_dsh_app(settings)

    assert application.state.default_agent_name == "assistant"
    assert set(application.state.agents) == {"assistant", "planner", "coder"}
    assert application.state.agents["planner"].definition.instructions == "Produce a plan."
    assert application.state.agents["coder"].provider == "deepseek-official"


def test_agent_registry_rejects_missing_default_agent(tmp_path) -> None:
    config_path = tmp_path / "agents.yaml"
    config_path.write_text(
        """\
default_agent: missing
agents:
  assistant:
    instructions: Answer.
    provider: deepseek-official
    model: deepseek-v4-flash
""",
        encoding="utf-8",
    )

    with pytest.raises(DshAgentConfigError, match="default_agent 'missing'"):
        load_dsh_agent_registry(config_path)


def test_fastapi_settings_load_dotenv_without_overriding_process_environment(
    tmp_path,
    monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "BASE_AGENT_HOST=0.0.0.0",
                "BASE_AGENT_PORT=9000",
                f"BASE_AGENT_WORKSPACE={tmp_path}",
                f"BASE_AGENT_CONFIG={tmp_path / 'agents.yaml'}",
                "DSH_PROVIDER=deepseek-official",
                "DSH_MODEL=from-dotenv",
                "DEEPSEEK_BASE_URL=https://gateway.example/v1",
                "DEEPSEEK_API_KEY=from-dotenv",
            )
        ),
        encoding="utf-8",
    )
    (tmp_path / "agents.yaml").write_text(
        """\
default_agent: assistant
agents:
  assistant:
    instructions: Answer.
    provider: deepseek-official
    model: deepseek-v4-flash
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    for name in (
        "BASE_AGENT_HOST",
        "BASE_AGENT_PORT",
        "BASE_AGENT_WORKSPACE",
        "BASE_AGENT_CONFIG",
        "DSH_PROVIDER",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DSH_MODEL", "from-process")

    settings = DshFastApiSettings.from_env()

    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.agent_config_path == (tmp_path / "agents.yaml").resolve()
    assert settings.provider == "deepseek-official"
    assert settings.model == "from-process"
    assert os.environ["DEEPSEEK_BASE_URL"] == "https://gateway.example/v1"
    assert os.environ["DEEPSEEK_API_KEY"] == "from-dotenv"


def test_sdk_runtime_process_cwd_is_isolated_from_agent_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    dsh_home = tmp_path / "dsh-home"
    dsh_home.mkdir()
    patch = tmp_path / "profile.patch.json"
    patch.write_text("[]", encoding="utf-8")
    harness_arguments: dict[str, object] = {}

    class Harness:
        def __init__(self, **kwargs) -> None:
            harness_arguments.update(kwargs)

    monkeypatch.setattr(
        dsh_runtime_module,
        "verify_dsh_versions",
        lambda expected: (expected, expected),
    )
    monkeypatch.setattr(
        dsh_runtime_module,
        "import_module",
        lambda name: SimpleNamespace(DeepSeekHarness=Harness),
    )
    factory = SdkRuntimeFactory(
        SdkRuntimeConfig(workspace=workspace),
        patch_resolver=lambda spec, home: (patch,),
    )
    spec = RuntimeSpec(
        agent_name="assistant",
        fingerprint="fingerprint",
        instructions="Answer.",
        tool_names=(),
        tool_gateway_url=None,
        context_configuration={"mode": "keep-all"},
        provider="deepseek-official",
        model="deepseek-v4-flash",
    )

    factory.create(spec, dsh_home=dsh_home)

    assert harness_arguments["cwd"] == str(workspace)
    assert harness_arguments["runtime_cwd"] == str(dsh_home)
