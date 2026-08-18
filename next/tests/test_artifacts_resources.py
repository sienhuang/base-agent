import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    ArtifactManager,
    ArtifactSpec,
    Flow,
    InMemoryArtifactStore,
    LocalArtifactStore,
    PlannerDecision,
    ResourceContext,
    ResourceSpec,
    RunStatus,
    Stage,
    StepOutcome,
    ToolChoice,
    ToolContext,
    tool,
)
from base_agent.artifacts import ArtifactAccessError
from base_agent.models import FinishReason, ModelResponse, ToolCall
from base_agent.testing import FakeModel


async def test_artifact_manager_commits_immutable_content_and_enforces_grants() -> None:
    store = InMemoryArtifactStore()
    producer = ArtifactManager(store, run_id="run-a", agent_id="agent-a")
    artifact = await producer.create(
        name="dataset",
        media_type="text/csv",
        content=b"value\n1\n",
    )

    assert artifact.size_bytes == 8
    assert await producer.read(artifact.id) == b"value\n1\n"
    with pytest.raises(ArtifactAccessError):
        await ArtifactManager(
            store,
            run_id="run-b",
            agent_id="agent-b",
        ).read(artifact.id)

    consumer = ArtifactManager(
        store,
        run_id="run-b",
        agent_id="agent-b",
        granted_artifact_ids=(artifact.id,),
    )
    assert await consumer.read_named("dataset") == b"value\n1\n"


async def test_harness_injects_services_and_releases_run_resource() -> None:
    lifecycle: list[str] = []

    @asynccontextmanager
    async def resource_factory(
        _context: ResourceContext,
    ) -> AsyncIterator[dict[str, str]]:
        lifecycle.append("acquired")
        yield {"prefix": "verified"}
        lifecycle.append("released")

    @tool
    async def produce(value: str, context: ToolContext) -> dict[str, str]:
        assert context.resources is not None
        assert context.artifacts is not None
        resource = await context.resources.get("workspace", dict)
        artifact = await context.artifacts.create(
            name="result",
            media_type="text/plain",
            content=f"{resource['prefix']}:{value}",
        )
        return {"artifact_id": artifact.id}

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="produce",
                        arguments={"value": "data"},
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="artifact-agent",
            version="1.0.0",
            instructions="Produce one Artifact.",
            tools=("produce",),
            tool_choice=ToolChoice.REQUIRED,
            max_model_turns=2,
        ),
        model=model,
        tools=(produce,),
        resources=(ResourceSpec("workspace", resource_factory, eager=True),),
    )

    result = await agent.run("produce data")

    assert result.status is RunStatus.SUCCEEDED
    assert lifecycle == ["acquired", "released"]
    assert len(result.artifacts) == 1
    assert result.artifacts[0].name == "result"
    assert result.artifacts[0].tool_call_id == "call-1"
    assert "context" not in produce.spec.input_schema["properties"]
    assert "artifact.created" in {event.kind for event in result.events}


async def test_run_cancellation_releases_acquired_resources() -> None:
    lifecycle: list[str] = []
    tool_started = asyncio.Event()

    @asynccontextmanager
    async def resource_factory(
        _context: ResourceContext,
    ) -> AsyncIterator[object]:
        lifecycle.append("acquired")
        yield object()
        lifecycle.append("released")

    @tool
    async def wait_forever(context: ToolContext) -> None:
        assert context.resources is not None
        await context.resources.get("workspace")
        tool_started.set()
        await asyncio.Event().wait()

    agent = Agent(
        AgentDefinition(
            id="cancellable-resource-agent",
            version="1.0.0",
            instructions="Wait.",
            tools=("wait_forever",),
            tool_choice=ToolChoice.REQUIRED,
            max_model_turns=2,
        ),
        model=FakeModel(
            (
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="wait-1", name="wait_forever", arguments={}),
                    ),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
            )
        ),
        tools=(wait_forever,),
        resources=(ResourceSpec("workspace", resource_factory),),
    )

    handle = agent.start("wait")
    await asyncio.wait_for(tool_started.wait(), timeout=1)
    result = await handle.cancel()

    assert result.status is RunStatus.CANCELLED
    assert lifecycle == ["acquired", "released"]


async def test_resource_cleanup_failure_prevents_success_commit() -> None:
    @asynccontextmanager
    async def broken_resource(
        _context: ResourceContext,
    ) -> AsyncIterator[object]:
        yield object()
        raise RuntimeError("close failed")

    agent = Agent(
        AgentDefinition(
            id="broken-cleanup-agent",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("would have succeeded"),
        resources=(ResourceSpec("broken", broken_resource, eager=True),),
    )

    result = await agent.run("answer")

    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == "resource_cleanup_failed"
    assert result.events[-1].kind == "run.failed"


async def test_local_artifact_store_round_trips_and_detects_tampering(
    tmp_path: Path,
) -> None:
    store = LocalArtifactStore(tmp_path)
    manager = ArtifactManager(store, run_id="run-local", agent_id="agent-local")
    created = await manager.create(
        name="report",
        media_type="text/markdown",
        content="# verified",
        metadata={"source": "test"},
    )

    reopened = LocalArtifactStore(tmp_path)
    assert await reopened.get(created.id) == created
    assert await reopened.read(created.id) == b"# verified"
    assert await reopened.list(run_id="run-local") == (created,)

    object_path = tmp_path / "objects" / f"{created.id}.bin"
    object_path.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        await reopened.read(created.id)


async def test_sequence_grants_only_upstream_artifact_references() -> None:
    store = InMemoryArtifactStore()

    @tool
    async def create_dataset(context: ToolContext) -> str:
        assert context.artifacts is not None
        artifact = await context.artifacts.create(
            name="dataset",
            media_type="text/csv",
            content="value\n1\n",
        )
        return artifact.id

    @tool
    async def create_report(context: ToolContext) -> str:
        assert context.artifacts is not None
        source = await context.artifacts.find("dataset")
        content = await context.artifacts.read(source.id)
        artifact = await context.artifacts.create(
            name="report",
            media_type="text/markdown",
            content=f"# Rows\n\n{content.decode()}",
            source_artifact_ids=(source.id,),
        )
        return artifact.id

    query_agent = Agent(
        AgentDefinition(
            id="query",
            version="1.0.0",
            instructions="Create data.",
            tools=("create_dataset",),
            tool_choice=ToolChoice.REQUIRED,
            max_model_turns=2,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="q1", name="create_dataset", arguments={}),
                    ),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
                ModelResponse(content="dataset ready"),
            ]
        ),
        tools=(create_dataset,),
        artifact_store=store,
    )
    analysis_agent = Agent(
        AgentDefinition(
            id="analysis",
            version="1.0.0",
            instructions="Create report.",
            tools=("create_report",),
            tool_choice=ToolChoice.REQUIRED,
            max_model_turns=2,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="a1", name="create_report", arguments={}),
                    ),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
                ModelResponse(content="report ready"),
            ]
        ),
        tools=(create_report,),
        artifact_store=store,
    )
    flow = Flow.sequence(
        id="artifact-flow",
        stages=(
            Stage(
                name="query",
                agent=query_agent,
                artifacts=(ArtifactSpec("dataset", "text/csv"),),
            ),
            Stage(
                name="analysis",
                agent=analysis_agent,
                input_from="query",
                artifacts=(ArtifactSpec("report", "text/markdown"),),
            ),
        ),
    )

    result = await flow.run("analyze")

    assert [artifact.name for artifact in result.artifacts] == ["dataset", "report"]
    dataset, report = result.artifacts
    assert result.stages[1].run.artifact_inputs == (dataset,)
    assert report.source_artifact_ids == (dataset.id,)
    assert report.flow_run_id == result.flow_run_id
    assert report.stage == "analysis"
    assert [binding.status for binding in result.artifact_bindings] == [
        "accepted",
        "accepted",
    ]


async def test_planner_react_aggregates_child_artifacts() -> None:
    store = InMemoryArtifactStore()

    @tool
    async def execute_step(context: ToolContext) -> str:
        assert context.artifacts is not None
        artifact = await context.artifacts.create(
            name="step_result",
            media_type="text/plain",
            content="checked result",
        )
        return artifact.id

    planner = Agent(
        AgentDefinition(
            id="artifact-planner",
            version="1.0.0",
            instructions="Plan and review.",
            response_model=PlannerDecision,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    content=(
                        '{"action":"create","goal":"produce result",'
                        '"steps":[{"description":"execute"}],'
                        '"reason":"one step","final_output":null}'
                    )
                ),
                ModelResponse(
                    content=(
                        '{"action":"complete","goal":"produce result",'
                        '"steps":[],"reason":"done",'
                        '"final_output":"checked result"}'
                    )
                ),
            ]
        ),
        artifact_store=store,
    )
    executor = Agent(
        AgentDefinition(
            id="artifact-executor",
            version="1.0.0",
            instructions="Execute.",
            tools=("execute_step",),
            tool_choice=ToolChoice.REQUIRED,
            response_model=StepOutcome,
            max_model_turns=2,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(id="execute-1", name="execute_step", arguments={}),
                    ),
                    finish_reason=FinishReason.TOOL_CALLS,
                ),
                ModelResponse(
                    content=(
                        '{"success":true,"summary":"executed",'
                        '"output":"checked result","error":null}'
                    )
                ),
            ]
        ),
        tools=(execute_step,),
        artifact_store=store,
    )

    result = await Flow.planner_react(
        id="artifact-plan",
        planner=planner,
        executor=executor,
    ).run("produce result")

    assert [artifact.name for artifact in result.artifacts] == ["step_result"]
    review_run = result.child_runs[-1].run
    assert review_run.artifact_inputs == result.artifacts
