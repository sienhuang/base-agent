from __future__ import annotations

import asyncio

import httpx

from dsh_base_agent import (
    AgentDefinition,
    Context,
    ContextBlock,
    ContextSource,
    DshApplication,
    KeepAllContextPolicy,
    ProviderConfig,
    Skill,
    SkillDependencies,
    tool,
)
from dsh_base_agent.testing import ScriptedRuntimeFactory


async def test_business_application_registers_capabilities_and_serves_http(tmp_path) -> None:
    @tool
    async def query_order(order_id: str) -> dict[str, str]:
        """Query one order."""

        return {"order_id": order_id}

    runtime = ScriptedRuntimeFactory({"order-agent": ["paid"]})
    provider = ProviderConfig(
        name="fast-model",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        base_url="https://gateway.example/v1",
        api_key="secret-value",
        max_tokens=2048,
    )
    service = DshApplication(
        workspace=tmp_path,
        state_dir=tmp_path / "state",
        providers=(provider,),
        runtime_factory=runtime,
    )
    service.register_context(
        Context(
            name="orders",
            policy=KeepAllContextPolicy(),
            blocks=(
                ContextBlock(
                    id="order-policy",
                    content="Only paid orders may be fulfilled.",
                    source=ContextSource.APPLICATION,
                    trusted=True,
                    token_estimate=8,
                ),
            ),
        ),
        default=True,
    )
    service.register_tool(query_order)
    service.register_skill(
        Skill(
            name="orders",
            playbook="Always query the order before answering.",
            dependencies=SkillDependencies(required_tools=("query_order",)),
        )
    )
    agent = service.register_agent(
        AgentDefinition(
            id="order-agent",
            version="1.0.0",
            instructions="Handle order questions.",
            skills=("orders",),
        ),
        default=True,
    )
    application = service.fastapi()

    assert service.agent("order-agent") is agent
    assert agent.runtime_spec.provider == "deepseek-official"
    assert agent.runtime_spec.model == "deepseek-v4-flash"
    assert agent.runtime_spec.max_tokens == 2048
    assert agent.runtime_spec.api_key == "secret-value"
    assert "secret-value" not in repr(agent.runtime_spec)
    assert application.state.dsh_application is service

    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            started = await client.post("/v1/runs", json={"prompt": "order A-100"})
            run_id = started.json()["run_id"]
            for _ in range(100):
                completed = await client.get(f"/v1/runs/{run_id}")
                if completed.json()["status"] != "running":
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("business Agent Run did not complete")

    assert completed.json()["status"] == "succeeded"
    assert completed.json()["output"] == "paid"
    assert "Only paid orders may be fulfilled." in runtime.calls[0].prompt
    assert "Always query the order before answering." in agent.runtime_spec.instructions


def test_provider_profiles_and_contexts_are_explicitly_selected(tmp_path) -> None:
    runtime = ScriptedRuntimeFactory({})
    service = DshApplication(
        workspace=tmp_path,
        providers=(
            ProviderConfig("fast", "deepseek-official", "deepseek-v4-flash"),
            ProviderConfig("quality", "custom-route", "quality-model"),
        ),
        default_provider="fast",
        runtime_factory=runtime,
    )
    service.register_context(Context(name="business"), default=True)
    fast = service.register_agent(
        AgentDefinition("fast-agent", "1.0.0", "Answer quickly."),
    )
    quality = service.register_agent(
        AgentDefinition("quality-agent", "1.0.0", "Answer carefully."),
        provider="quality",
    )

    assert fast.runtime_spec.model == "deepseek-v4-flash"
    assert quality.runtime_spec.provider == "custom-route"
    assert quality.runtime_spec.model == "quality-model"
    assert service.contexts["business"].policy.name == "keep-all"
