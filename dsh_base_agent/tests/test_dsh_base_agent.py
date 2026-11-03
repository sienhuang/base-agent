from __future__ import annotations

import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

from dsh_base_agent import (
    Agent,
    AgentDefinition,
    AllowAllConfirmations,
    BaseAgentApp,
    ContextBlock,
    ContextSource,
    Flow,
    FlowStatus,
    InMemoryMemory,
    KeepAllContextPolicy,
    LocalState,
    MemoryAugmentedContextPolicy,
    MemoryScope,
    MemoryWrite,
    PlannerAction,
    PlannerDecision,
    RunStatus,
    Skill,
    Stage,
    StepOutcome,
    ToolChoice,
    tool,
)
from dsh_base_agent._dsh import GeneratedSdkProfilePatches
from dsh_base_agent.testing import ScriptedRuntimeFactory
from dsh_base_agent.tool_gateway import ToolGateway


def test_framework_contracts_are_owned_by_dsh_base_agent() -> None:
    assert AgentDefinition.__module__ == "dsh_base_agent.definitions"
    assert RunStatus.__module__ == "dsh_base_agent.models"
    assert Flow.__module__ == "dsh_base_agent.orchestration"
    package_root = Path(__file__).parents[1]
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (package_root / "src" / "dsh_base_agent").rglob("*.py")
    )
    project = (package_root / "pyproject.toml").read_text(encoding="utf-8")
    assert "from base_agent" not in source
    assert "import base_agent" not in source
    assert "base-agent-next" not in project


def _agent(
    app: BaseAgentApp,
    agent_id: str,
    *,
    response_model: type[BaseModel] | None = None,
) -> Agent:
    return Agent(
        AgentDefinition(
            id=agent_id,
            version="1.0.0",
            instructions=f"Act as {agent_id}.",
            response_model=response_model,
        ),
        app=app,
    )


async def test_agent_returns_local_run_result_over_dsh(tmp_path) -> None:
    runtime = ScriptedRuntimeFactory({"assistant": ["hello from DSH"]})
    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=runtime,
    )
    agent = _agent(app, "assistant")

    result = await agent.run("hello")

    assert result.status is RunStatus.SUCCEEDED
    assert result.output == "hello from DSH"
    assert result.text == "hello from DSH"
    assert runtime.calls[0].prompt == "hello"
    assert [event.kind for event in result.events] == [
        "run.started",
        "message.created",
        "context.decision.started",
        "context.decision.committed",
        "context.prepared",
        "dsh.turn.start",
        "dsh.step.start",
        "dsh.assistant.message",
        "dsh.step.end",
        "dsh.turn.end",
        "message.completed",
        "run.succeeded",
    ]
    await app.close()


async def test_context_and_memory_are_selected_into_the_real_dsh_input(tmp_path) -> None:
    memory = InMemoryMemory()
    runtime = ScriptedRuntimeFactory({"assistant": ["paid"]})
    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=runtime,
        memory=memory,
    )
    await app.memory.write(
        "memory-setup",
        MemoryWrite(
            tenant="tenant-a",
            namespace="orders",
            content="Order A-100 is paid",
            source_event_ref="orders:1",
            idempotency_key="order-a-100",
        ),
    )
    policy = MemoryAugmentedContextPolicy(
        KeepAllContextPolicy(),
        minimum_score=0.5,
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer from business context.",
        ),
        app=app,
        context_policy=policy,
        context_blocks=(
            ContextBlock(
                id="order-policy",
                content="Paid orders may be fulfilled.",
                source=ContextSource.APPLICATION,
                trusted=True,
                token_estimate=8,
            ),
        ),
        memory_scope=MemoryScope(("tenant-a", "orders")),
    )

    result = await agent.run("A-100")

    assert result.status is RunStatus.SUCCEEDED
    submitted = runtime.calls[0].prompt
    assert "Paid orders may be fulfilled" in submitted
    assert "Order A-100 is paid" in submitted
    prepared = next(event for event in result.events if event.kind == "context.prepared")
    assert prepared.payload["context_refs"] == ["order-policy"]
    assert len(prepared.payload["memory_refs"]) == 1
    await app.close()


async def test_python_tool_is_callable_through_the_loopback_mcp_gateway(tmp_path) -> None:
    @tool
    async def multiply(left: int, right: int) -> dict[str, int]:
        """Multiply two integers."""

        return {"product": left * right}

    state = LocalState(tmp_path / "state")
    gateway = ToolGateway(
        agent_id="calculator",
        tools=(multiply,),
        event_store=state.event_store,
        confirmation_policy=AllowAllConfirmations(),
        max_tool_calls=1,
    )
    await gateway.start()
    gateway.bind_run("run-tool")
    try:
        async with streamable_http_client(gateway.url) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                listed = await session.list_tools()
                result = await session.call_tool(
                    "multiply",
                    {"left": 6, "right": 7},
                )
                over_budget = await session.call_tool(
                    "multiply",
                    {"left": 2, "right": 3},
                )
    finally:
        gateway.release_run("run-tool")
        await gateway.close()

    assert [item.name for item in listed.tools] == ["multiply"]
    assert result.structuredContent == {"product": 42}
    assert over_budget.isError is True
    events = await state.event_store.list("run-tool")
    assert [event.kind for event in events] == [
        "tool.started",
        "tool.completed",
        "tool.started",
        "tool.completed",
    ]
    assert events[-1].payload["status"] == "error"


async def test_profile_connects_selected_tools_and_disables_unselected_native_tools(
    tmp_path,
) -> None:
    @tool
    async def lookup(value: str) -> dict[str, str]:
        """Look up one value."""

        return {"value": value}

    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=ScriptedRuntimeFactory({}),
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Use business tools.",
            tools=("lookup",),
        ),
        app=app,
        tools=(lookup,),
    )

    (patch_path,) = GeneratedSdkProfilePatches()(
        agent.runtime_spec,
        app.state.runtime_home(agent.fingerprint),
    )
    patch = json.loads(patch_path.read_text(encoding="utf-8"))

    assert {item.get("id") for item in patch if item.get("disabled")} >= {
        "plan-mode",
        "tool-bash",
        "tool-fs",
        "tool-subagent-list-agents",
        "tool-web",
    }
    inserted = next(item["insert"][0] for item in patch if "insert" in item)
    assert inserted["name"] == "@deepseek-ai/dsh-mcp-client"
    assert inserted["config"]["url"] == agent.runtime_spec.tool_gateway_url
    await app.close()


async def test_required_tool_choice_fails_when_dsh_does_not_call_a_tool(tmp_path) -> None:
    @tool
    async def lookup(value: str) -> dict[str, str]:
        """Look up one value."""

        return {"value": value}

    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=ScriptedRuntimeFactory({"assistant": ["unguarded answer"]}),
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Use the lookup Tool.",
            tools=("lookup",),
            tool_choice=ToolChoice.REQUIRED,
        ),
        app=app,
        tools=(lookup,),
    )

    result = await agent.run("value")

    assert result.status is RunStatus.FAILED
    assert "without a successful Tool call" in (result.error or "")
    await app.close()


async def test_skill_playbook_is_compiled_into_the_dsh_persona(tmp_path) -> None:
    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=ScriptedRuntimeFactory({"assistant": ["done"]}),
    )
    skill = Skill(
        name="orders",
        version="2.0.0",
        description="Order handling rules",
        playbook="Always verify payment before fulfillment.",
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Handle orders.",
            skills=("orders",),
        ),
        app=app,
        skills=(skill,),
    )

    assert "# Business skills" in agent.runtime_spec.instructions
    assert "Always verify payment before fulfillment" in agent.runtime_spec.instructions
    assert agent.snapshot.skill_names == ("orders",)
    await app.close()


async def test_sequential_flow_composes_independent_dsh_runs(tmp_path) -> None:
    runtime = ScriptedRuntimeFactory({"analyze": ["analysis"], "review": ["approved"]})
    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=runtime,
    )
    analyze = _agent(app, "analyze")
    review = _agent(app, "review")
    flow = Flow.sequence(
        id="review-flow",
        stages=(
            Stage(name="analyze", agent=analyze),
            Stage(name="review", agent=review, input_from="analyze"),
        ),
        event_store=app.event_store,
    )

    result = await flow.run("request")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.output == "approved"
    assert runtime.calls[1].prompt == "analysis"
    assert result.events[0].kind == "flow.started"
    assert result.events[-1].kind == "flow.succeeded"
    await app.close()


async def test_planner_react_flow_uses_dsh_agents(tmp_path) -> None:
    create = PlannerDecision(
        action=PlannerAction.CREATE,
        goal="Resolve the case",
        steps=({"description": "Inspect the case"},),
        reason="start",
        final_output=None,
    ).model_dump_json()
    complete = PlannerDecision(
        action=PlannerAction.COMPLETE,
        goal=None,
        steps=(),
        reason="done",
        final_output="case resolved",
    ).model_dump_json()
    outcome = StepOutcome(
        success=True,
        summary="inspection complete",
        output="evidence",
        error=None,
        artifact_ids=(),
    ).model_dump_json()
    runtime = ScriptedRuntimeFactory({"planner": [create, complete], "executor": [outcome]})
    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=runtime,
    )
    planner = _agent(app, "planner", response_model=PlannerDecision)
    executor = _agent(app, "executor", response_model=StepOutcome)
    flow = Flow.planner_react(
        id="case-flow",
        planner=planner,
        executor=executor,
        event_store=app.event_store,
    )

    result = await flow.run("help")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.output == "case resolved"
    assert [child.role.value for child in result.child_runs] == [
        "planner",
        "executor",
        "planner",
    ]
    await app.close()


async def test_invalid_structured_output_is_a_failed_run_result(tmp_path) -> None:
    class Answer(BaseModel):
        value: int

    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=ScriptedRuntimeFactory({"assistant": ["not-json"]}),
    )
    agent = _agent(app, "assistant", response_model=Answer)

    result = await agent.run("answer")

    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == "dsh_run_failed"
    assert result.events[-1].kind == "run.failed"
    await app.close()


async def test_model_step_budget_closes_an_overrunning_dsh_runtime(tmp_path) -> None:
    class OverflowRuntime:
        def __init__(self) -> None:
            self.cancelled = False

        async def start(self) -> None:
            return None

        async def run(self, input, *, session_id, on_event=None):
            del input, session_id
            assert on_event is not None
            await on_event({"type": "step/start", "data": {"turn": 1, "step": 1}})
            await on_event({"type": "step/start", "data": {"turn": 1, "step": 2}})
            raise AssertionError("the Host budget must stop the DSH adapter")

        async def cancel(self, session_id: str) -> None:
            del session_id
            self.cancelled = True

        async def close(self) -> None:
            self.cancelled = True

    class OverflowFactory:
        def __init__(self) -> None:
            self.runtime = OverflowRuntime()

        def create(self, spec, *, dsh_home):
            del spec
            dsh_home.mkdir(parents=True, exist_ok=True)
            return self.runtime

    factory = OverflowFactory()
    app = BaseAgentApp(
        state=LocalState(tmp_path / "state"),
        runtime_factory=factory,
    )
    agent = Agent(
        AgentDefinition(
            id="bounded",
            version="1.0.0",
            instructions="Stay within the budget.",
            max_model_turns=1,
        ),
        app=app,
    )

    result = await agent.run("work")

    assert result.status is RunStatus.FAILED
    assert factory.runtime.cancelled is True
    assert [event.kind for event in result.events][-2:] == [
        "run.budget.exceeded",
        "run.failed",
    ]
    await app.close()
