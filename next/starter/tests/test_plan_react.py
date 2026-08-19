from base_agent import InMemoryArtifactStore
from base_agent.events import InMemoryEventStore
from base_agent.flows import (
    ChildRunRole,
    FlowStatus,
    PlannerDecision,
    StepOutcome,
)
from base_agent.models import ModelRequest, ModelResponse, ToolResultStatus

from agent_app.app import (
    PLAN_REACT_EXECUTOR_DEFINITION,
    PLAN_REACT_PLANNER_DEFINITION,
    build_plan_react_flow,
    run_plan_react,
)
from agent_app.provider import OfflineProvider


class CapturingOfflineProvider(OfflineProvider):
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return await super().complete(request)


def test_plan_react_composition_uses_fixed_structured_roles() -> None:
    artifact_store = InMemoryArtifactStore()
    event_store = InMemoryEventStore()
    flow = build_plan_react_flow(
        artifact_store=artifact_store,
        event_store=event_store,
    )

    assert flow.planner.definition is PLAN_REACT_PLANNER_DEFINITION
    assert flow.executor.definition is PLAN_REACT_EXECUTOR_DEFINITION
    assert flow.planner.definition.response_model is PlannerDecision
    assert flow.executor.definition.response_model is StepOutcome
    assert flow.planner.definition.tools == ()
    assert flow.planner.definition.skills == ()
    assert flow.executor.definition.tools
    assert flow.executor.definition.skills
    assert flow.planner.artifact_store is artifact_store
    assert flow.executor.artifact_store is artifact_store
    assert flow.event_store is event_store
    assert flow.planner.event_store is event_store
    assert flow.executor.event_store is event_store
    assert OfflineProvider().info.capabilities.structured_output is True


async def test_plan_react_routes_planner_and_executor_models_independently() -> None:
    provider = CapturingOfflineProvider()
    result = await run_plan_react(
        "calculate 4 * 30",
        model=provider,
        planner_model="planner-model",
        executor_model="executor-model",
        artifact_store=InMemoryArtifactStore(),
    )

    assert result.status is FlowStatus.SUCCEEDED
    assert [request.model for request in provider.requests] == [
        "planner-model",
        "executor-model",
        "planner-model",
    ]


async def test_plan_react_uses_independent_role_providers() -> None:
    planner_provider = CapturingOfflineProvider()
    executor_provider = CapturingOfflineProvider()

    result = await run_plan_react(
        "calculate 4 * 30",
        planner_provider=planner_provider,
        executor_provider=executor_provider,
        artifact_store=InMemoryArtifactStore(),
    )

    assert result.status is FlowStatus.SUCCEEDED
    assert len(planner_provider.requests) == 2
    assert len(executor_provider.requests) == 1


async def test_offline_plan_react_runs_create_execute_review_to_completion() -> None:
    result = await run_plan_react(
        "calculate 4 * 30",
        artifact_store=InMemoryArtifactStore(),
    )

    assert result.status is FlowStatus.SUCCEEDED
    assert result.error is None
    assert result.plan is not None
    assert result.plan.status.value == "succeeded"
    assert result.output.startswith("Offline Executor completed:")
    assert [child.role for child in result.child_runs] == [
        ChildRunRole.PLANNER,
        ChildRunRole.EXECUTOR,
        ChildRunRole.PLANNER,
    ]
    assert [
        event.kind
        for event in result.events
        if event.payload.get("event_scope") != "child_run"
    ] == [
        "flow.started",
        "message.created",
        "planner.started",
        "planner.completed",
        "plan.created",
        "step.started",
        "step.completed",
        "planner.started",
        "planner.completed",
        "plan.reviewed",
        "plan.completed",
        "message.completed",
        "flow.succeeded",
    ]
    child_events = [
        event
        for event in result.events
        if event.payload.get("event_scope") == "child_run"
    ]
    assert {event.payload["child_role"] for event in child_events} == {
        "planner",
        "executor",
    }
    assert all(event.payload["agent_run_id"] for event in child_events)
    assert all(event.payload["child_sequence"] >= 1 for event in child_events)
    assert "model.responded" in {event.kind for event in child_events}
    assert "child.run.succeeded" in {event.kind for event in child_events}


async def test_plan_react_streams_child_json_only_as_internal_deltas() -> None:
    observed = []

    result = await run_plan_react(
        "calculate 4 * 30",
        stream=True,
        on_event=observed.append,
        artifact_store=InMemoryArtifactStore(),
    )

    assert result.status is FlowStatus.SUCCEEDED
    deltas = [event for event in observed if event.kind == "child.message.delta"]
    assert deltas
    assert all(event.payload["event_scope"] == "child_run" for event in deltas)
    assert {event.payload["child_role"] for event in deltas} == {
        "planner",
        "executor",
    }
    assert all(isinstance(event.payload["delta"], str) for event in deltas)
    assert not [event for event in observed if event.kind == "message.delta"]
    final_message = next(
        event
        for event in observed
        if event.kind == "message.completed"
        and event.payload.get("event_scope") == "flow"
    )
    assert final_message.payload["content"] == result.output


async def test_offline_plan_react_executor_can_use_starter_skill_tool() -> None:
    observed = []
    result = await run_plan_react(
        'tool=calculate_product calls=1 tool_args={"left":4,"right":30}',
        stream=True,
        on_event=observed.append,
        artifact_store=InMemoryArtifactStore(),
    )

    assert result.status is FlowStatus.SUCCEEDED
    executor_child = next(
        child for child in result.child_runs if child.role is ChildRunRole.EXECUTOR
    )
    assert len(executor_child.run.tool_results) == 1
    tool_result = executor_child.run.tool_results[0]
    assert tool_result.call.name == "calculate_product"
    assert tool_result.status is ToolResultStatus.SUCCESS
    assert tool_result.data == {"product": 120}
    assert '"product":120' in result.output
    assert not [
        event
        for event in observed
        if event.kind in {"model.tool_call.delta", "child.model.tool_call.delta"}
        or event.payload.get("child_kind") == "model.tool_call.delta"
    ]


async def test_plan_react_selects_recorded_value_artifact() -> None:
    observed = []
    artifact_store = InMemoryArtifactStore()

    result = await run_plan_react(
        'tool=record_value calls=1 tool_args={"value":42}',
        stream=True,
        on_event=observed.append,
        artifact_store=artifact_store,
    )

    assert result.status is FlowStatus.SUCCEEDED
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert artifact.name == "recorded_value"
    assert artifact.media_type == "application/json"
    assert await artifact_store.read(artifact.id) == b'{"value":42}'
    executor_run = result.child_runs[1].run
    review_run = result.child_runs[2].run
    assert executor_run.artifacts == (artifact,)
    assert review_run.artifact_inputs == ()
    assert [
        event.kind
        for event in observed
        if event.kind in {"artifact.created", "artifact.selected", "artifact.granted"}
    ] == ["artifact.created", "artifact.selected"]
