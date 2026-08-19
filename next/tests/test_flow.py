import asyncio
import json

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    Event,
    Flow,
    FlowRunHandle,
    FlowStatus,
    PlannerDecision,
    PlannerReActResult,
    PlanningLimits,
    PlanStepDraft,
    RetryPolicy,
    RunStatus,
    Stage,
    StepOutcome,
    VerificationEvidence,
)
from base_agent.flows import (
    ChildRunRole,
    PlanStatus,
    PlanStepStatus,
)
from base_agent.models import ModelRequest, ModelResponse
from base_agent.providers import ProviderCapabilities, ProviderInfo
from base_agent.testing import FakeModel


def _json_response(data: object) -> ModelResponse:
    return ModelResponse(content=json.dumps(data))


def _planning_agents(
    *,
    planner_responses: list[ModelResponse],
    executor_responses: list[ModelResponse],
) -> tuple[Agent, Agent, FakeModel, FakeModel]:
    planner_model = FakeModel(planner_responses, name="planner-model")
    executor_model = FakeModel(executor_responses, name="executor-model")
    planner = Agent(
        AgentDefinition(
            id="planner",
            version="1.0.0",
            instructions="Create and review Plans.",
            response_model=PlannerDecision,
        ),
        model=planner_model,
    )
    executor = Agent(
        AgentDefinition(
            id="executor",
            version="1.0.0",
            instructions="Execute exactly one Plan Step.",
            description="Can execute research and analysis Steps.",
            response_model=StepOutcome,
        ),
        model=executor_model,
    )
    return planner, executor, planner_model, executor_model


async def test_sequential_flow_passes_previous_stage_output_to_the_next_agent() -> None:
    query_model = FakeModel.text("revenue=1240000,growth=12.4%")
    analysis_model = FakeModel.text("Revenue grew 12.4% to 1.24M.")
    query_agent = Agent(
        AgentDefinition(
            id="query-agent",
            version="1.0.0",
            instructions="Fetch data.",
        ),
        model=query_model,
    )
    analysis_agent = Agent(
        AgentDefinition(
            id="analysis-agent",
            version="1.0.0",
            instructions="Analyze data.",
        ),
        model=analysis_model,
    )
    flow = Flow.sequence(
        id="analysis",
        stages=(
            Stage(
                name="query",
                agent=query_agent,
                prompt="Create a compact dataset.",
            ),
            Stage(
                name="analysis",
                agent=analysis_agent,
                prompt="Explain the following dataset.",
                input_from="query",
                verify=lambda run: VerificationEvidence(
                    verifier="report_non_empty",
                    scope="stage_output",
                    status="passed" if bool(run.text.strip()) else "failed",
                ),
            ),
        ),
    )

    result = await flow.run("Analyze Q2 revenue")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.output == "Revenue grew 12.4% to 1.24M."
    assert (
        analysis_model.requests[0].messages[0].content
        == "Explain the following dataset.\n\nInput:\nrevenue=1240000,growth=12.4%"
    )
    assert [event.kind for event in result.events] == [
        "flow.started",
        "stage.started",
        "stage.succeeded",
        "stage.started",
        "stage.succeeded",
        "flow.succeeded",
    ]
    assert result.stages[1].evidence[0].status == "passed"


async def test_sequential_flow_retries_with_host_verification_evidence() -> None:
    model = FakeModel(
        [
            ModelResponse(content="empty dataset"),
            ModelResponse(content="checked dataset"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="retrying-query",
            version="1.0.0",
            instructions="Produce a checked dataset.",
        ),
        model=model,
    )
    flow = Flow.sequence(
        id="verified-query",
        stages=(
            Stage(
                name="query",
                agent=agent,
                verify=lambda run: VerificationEvidence(
                    verifier="dataset_non_empty",
                    scope="stage_output",
                    status=("passed" if run.text == "checked dataset" else "failed"),
                    detail=("CSV has no data rows" if run.text != "checked dataset" else ""),
                ),
                retry=RetryPolicy(max_attempts=2),
            ),
        ),
    )

    result = await flow.run("Query recent users")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.output == "checked dataset"
    assert [attempt.attempt for attempt in result.stage_attempts] == [1, 2]
    assert result.stages[0].attempt == 2
    assert result.stage_attempts[0].run.run_id != result.stage_attempts[1].run.run_id
    assert "Host verification rejected the previous attempt" in (
        model.requests[1].messages[0].content
    )
    assert "CSV has no data rows" in model.requests[1].messages[0].content
    assert "stage.retrying" in {event.kind for event in result.events}


async def test_planner_react_correlates_every_plan_phase_with_an_agent_run() -> None:
    planner, executor, planner_model, executor_model = _planning_agents(
        planner_responses=[
            _json_response(
                {
                    "action": "create",
                    "goal": "Produce a checked revenue explanation",
                    "steps": [
                        {"description": "Collect the revenue values"},
                        {"description": "Explain the growth"},
                    ],
                    "reason": "Two checked Steps are required",
                    "final_output": None,
                }
            ),
            _json_response(
                {
                    "action": "continue",
                    "goal": None,
                    "steps": [],
                    "reason": "The first result is sufficient",
                    "final_output": None,
                }
            ),
            _json_response(
                {
                    "action": "complete",
                    "goal": "Produce a checked revenue explanation",
                    "steps": [],
                    "reason": "All required work is complete",
                    "final_output": "Revenue grew 12.4% to 1.24M.",
                }
            ),
        ],
        executor_responses=[
            _json_response(
                {
                    "success": True,
                    "summary": "Collected revenue values",
                    "output": "revenue=1240000,growth=12.4%",
                    "error": None,
                    "artifact_ids": [],
                }
            ),
            _json_response(
                {
                    "success": True,
                    "summary": "Explained the growth",
                    "output": "Revenue grew 12.4%.",
                    "error": None,
                    "artifact_ids": [],
                }
            ),
        ],
    )
    flow = Flow.planner_react(
        id="revenue-analysis",
        planner=planner,
        executor=executor,
    )

    result = await flow.run("Analyze Q2 revenue")

    assert isinstance(result, PlannerReActResult)
    assert result.status is FlowStatus.SUCCEEDED
    assert result.output == "Revenue grew 12.4% to 1.24M."
    assert result.plan is not None
    assert result.plan.id == result.plan_id
    assert result.plan.revision == 1
    assert result.plan.status is PlanStatus.SUCCEEDED
    assert [step.status for step in result.plan.steps] == [
        PlanStepStatus.SUCCEEDED,
        PlanStepStatus.SUCCEEDED,
    ]
    assert [child.role for child in result.child_runs] == [
        ChildRunRole.PLANNER,
        ChildRunRole.EXECUTOR,
        ChildRunRole.PLANNER,
        ChildRunRole.EXECUTOR,
        ChildRunRole.PLANNER,
    ]
    assert len({child.agent_run_id for child in result.child_runs}) == 5
    assert all(child.flow_run_id == result.flow_run_id for child in result.child_runs)
    assert all(child.plan_id == result.plan_id for child in result.child_runs)
    assert result.child_runs[0].source_plan_revision is None
    assert result.child_runs[0].target_plan_revision == 1
    assert result.child_runs[1].source_plan_revision == 1
    assert result.child_runs[1].step_id == result.plan.steps[0].id
    assert result.child_runs[1].attempt == 1
    assert result.child_runs[2].source_plan_revision == 1
    assert result.child_runs[2].target_plan_revision == 1
    assert result.child_runs[3].step_id == result.plan.steps[1].id
    assert all(child.run.run_id == child.agent_run_id for child in result.child_runs)
    assert all(child.run.status is RunStatus.SUCCEEDED for child in result.child_runs)

    child_ids_from_events = {
        event.payload["agent_run_id"]
        for event in result.events
        if event.kind in {"planner.started", "step.started"}
    }
    assert child_ids_from_events == {child.agent_run_id for child in result.child_runs}
    assert [event.kind for event in result.events] == [
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
        "step.started",
        "step.completed",
        "planner.started",
        "planner.completed",
        "plan.reviewed",
        "plan.completed",
        "message.completed",
        "flow.succeeded",
    ]
    assert planner_model.requests[0].response_schema == PlannerDecision.model_json_schema()
    assert executor_model.requests[0].response_schema == StepOutcome.model_json_schema()
    assert set(PlannerDecision.model_json_schema()["required"]) == {
        "action",
        "goal",
        "steps",
        "reason",
        "final_output",
    }
    assert set(StepOutcome.model_json_schema()["required"]) == {
        "success",
        "summary",
        "output",
        "error",
        "artifact_ids",
    }
    assert StepOutcome.model_json_schema()["properties"]["output"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "title": "Output",
    }
    assert StepOutcome.model_json_schema()["properties"]["artifact_ids"] == {
        "items": {"type": "string"},
        "title": "Artifact Ids",
        "type": "array",
    }
    assert "Fixed Executor capabilities" in planner_model.requests[0].messages[0].content
    assert "Current Step id:" in executor_model.requests[0].messages[0].content


def test_step_outcome_rejects_invalid_artifact_selections() -> None:
    with pytest.raises(ValueError, match="blank ids"):
        StepOutcome(
            success=True,
            summary="done",
            output=None,
            error=None,
            artifact_ids=("",),
        )
    with pytest.raises(ValueError, match="duplicates"):
        StepOutcome(
            success=True,
            summary="done",
            output=None,
            error=None,
            artifact_ids=("artifact-1", "artifact-1"),
        )


async def test_planner_react_rejects_review_that_changes_plan_goal() -> None:
    planner, executor, _, _ = _planning_agents(
        planner_responses=[
            _json_response(
                {
                    "action": "create",
                    "goal": "Calculate the requested product",
                    "steps": [{"description": "Calculate 4 * 30"}],
                    "reason": "One calculation Step is sufficient",
                    "final_output": None,
                }
            ),
            _json_response(
                {
                    "action": "complete",
                    "goal": "Save the requested product",
                    "steps": [],
                    "reason": "The calculation is complete",
                    "final_output": "120",
                }
            ),
        ],
        executor_responses=[
            _json_response(
                {
                    "success": True,
                    "summary": "Calculated the product",
                    "output": "120",
                    "error": None,
                    "artifact_ids": [],
                }
            )
        ],
    )
    flow = Flow.planner_react(id="immutable-goal", planner=planner, executor=executor)

    result = await flow.run("Calculate 4 * 30")

    assert result.status is FlowStatus.FAILED
    assert result.plan is not None
    assert result.plan.goal == "Calculate the requested product"
    assert result.plan.status is PlanStatus.FAILED
    assert "attempted to change the Plan goal" in (result.error or "")
    assert [event.kind for event in result.events[-3:]] == [
        "planner.completed",
        "plan.failed",
        "flow.failed",
    ]


async def test_planner_react_preserves_terminal_prefix_when_revising_plan() -> None:
    planner, executor, _, _ = _planning_agents(
        planner_responses=[
            _json_response(
                {
                    "action": "create",
                    "goal": "Recover and finish",
                    "steps": [
                        {"description": "Try the primary source"},
                        {"description": "Use the original follow-up"},
                    ],
                    "reason": "Try the primary path before fallback",
                    "final_output": None,
                }
            ),
            _json_response(
                {
                    "action": "revise",
                    "goal": None,
                    "reason": "The primary source failed",
                    "steps": [{"description": "Use the fallback source"}],
                    "final_output": None,
                }
            ),
            _json_response(
                {
                    "action": "complete",
                    "goal": None,
                    "steps": [],
                    "reason": "The fallback completed the goal",
                    "final_output": "Recovered through the fallback source.",
                }
            ),
        ],
        executor_responses=[
            _json_response(
                {
                    "success": False,
                    "summary": "Primary source was unavailable",
                    "output": None,
                    "error": "source unavailable",
                    "artifact_ids": [],
                }
            ),
            _json_response(
                {
                    "success": True,
                    "summary": "Fallback source succeeded",
                    "output": "recovered",
                    "error": None,
                    "artifact_ids": [],
                }
            ),
        ],
    )
    flow = Flow.planner_react(id="recovery", planner=planner, executor=executor)

    result = await flow.run("Complete the task")

    assert result.status is FlowStatus.SUCCEEDED
    assert result.plan is not None
    assert result.plan.revision == 2
    assert [step.description for step in result.plan.steps] == [
        "Try the primary source",
        "Use the fallback source",
    ]
    assert [step.status for step in result.plan.steps] == [
        PlanStepStatus.FAILED,
        PlanStepStatus.SUCCEEDED,
    ]
    assert result.plan.steps[0].outcome is not None
    assert result.plan.steps[0].outcome.error == "source unavailable"
    assert result.child_runs[2].role is ChildRunRole.PLANNER
    assert result.child_runs[2].source_plan_revision == 1
    assert result.child_runs[2].target_plan_revision == 2
    revised = next(event for event in result.events if event.kind == "plan.revised")
    assert revised.payload["source_plan_revision"] == 1
    assert revised.payload["target_plan_revision"] == 2
    assert revised.payload["planner_run_id"] == result.child_runs[2].agent_run_id


async def test_planner_react_fails_when_child_run_fails_schema_validation() -> None:
    planner, executor, _, _ = _planning_agents(
        planner_responses=[
            _json_response(
                {
                    "action": "create",
                    "goal": "Execute one checked Step",
                    "steps": [{"description": "Return a structured result"}],
                    "reason": "One execution Step is required",
                    "final_output": None,
                }
            )
        ],
        executor_responses=[ModelResponse(content="not-json")],
    )
    flow = Flow.planner_react(id="invalid-output", planner=planner, executor=executor)

    result = await flow.run("Run it")

    assert result.status is FlowStatus.FAILED
    assert result.plan is not None
    assert result.plan.status is PlanStatus.FAILED
    assert result.plan.steps[0].status is PlanStepStatus.FAILED
    assert "failed response schema validation" in (result.error or "")
    assert result.child_runs[-1].role is ChildRunRole.EXECUTOR
    assert result.child_runs[-1].run.status is RunStatus.FAILED
    assert [event.kind for event in result.events[-3:]] == [
        "step.failed",
        "plan.failed",
        "flow.failed",
    ]


async def test_planner_react_enforces_plan_limits_before_execution() -> None:
    planner, executor, _, executor_model = _planning_agents(
        planner_responses=[
            _json_response(
                {
                    "action": "create",
                    "goal": "Too many Steps",
                    "steps": [
                        {"description": "First"},
                        {"description": "Second"},
                    ],
                    "reason": "The model proposed two Steps",
                    "final_output": None,
                }
            )
        ],
        executor_responses=[],
    )
    flow = Flow.planner_react(
        id="bounded",
        planner=planner,
        executor=executor,
        limits=PlanningLimits(max_steps=1, max_revisions=1, max_child_runs=3),
    )

    result = await flow.run("Do bounded work")

    assert result.status is FlowStatus.FAILED
    assert result.plan is None
    assert "exceeding max_steps=1" in (result.error or "")
    assert len(result.child_runs) == 1
    assert executor_model.requests == ()


class GatedStructuredModel:
    def __init__(self) -> None:
        self.requested = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="gated-structured-model",
            default_model="gated-structured-model",
            capabilities=ProviderCapabilities(structured_output=True),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requested.set()
        await self.release.wait()
        return _json_response(
            {
                "success": True,
                "summary": "Completed after release",
                "output": None,
                "error": None,
                "artifact_ids": [],
            }
        )


async def test_planner_react_cancel_propagates_to_the_active_child_run() -> None:
    planner_model = FakeModel(
        [
            _json_response(
                {
                    "action": "create",
                    "goal": "Wait for the executor",
                    "steps": [{"description": "Block until cancelled"}],
                    "reason": "One blocking Step",
                    "final_output": None,
                }
            )
        ]
    )
    executor_model = GatedStructuredModel()
    planner = Agent(
        AgentDefinition(
            id="cancel-planner",
            version="1.0.0",
            instructions="Plan.",
            response_model=PlannerDecision,
        ),
        model=planner_model,
    )
    executor = Agent(
        AgentDefinition(
            id="cancel-executor",
            version="1.0.0",
            instructions="Execute.",
            response_model=StepOutcome,
        ),
        model=executor_model,
    )
    flow = Flow.planner_react(id="cancel-flow", planner=planner, executor=executor)

    handle = flow.start("Wait")
    assert isinstance(handle, FlowRunHandle)
    await asyncio.wait_for(executor_model.requested.wait(), timeout=1)
    subscriber = asyncio.create_task(_collect_flow_events(handle))
    await asyncio.sleep(0)

    result = await handle.cancel()

    assert result.status is FlowStatus.CANCELLED
    assert result.error == "flow cancellation requested"
    assert result.plan is not None
    assert result.plan.status is PlanStatus.CANCELLED
    assert result.plan.steps[0].status is PlanStepStatus.CANCELLED
    assert result.child_runs[-1].role is ChildRunRole.EXECUTOR
    assert result.child_runs[-1].run.status is RunStatus.CANCELLED
    assert result.child_runs[-1].agent_run_id == result.child_runs[-1].run.run_id
    assert [event.kind for event in result.events[-3:]] == [
        "step.cancelled",
        "plan.cancelled",
        "flow.cancelled",
    ]
    assert await subscriber == result.events
    assert await handle.wait() is result
    assert handle.status is FlowStatus.CANCELLED


async def _collect_flow_events(
    handle: FlowRunHandle[PlannerReActResult],
) -> tuple[Event, ...]:
    return tuple([event async for event in handle.stream()])


async def test_direct_flow_cancellation_propagates_after_cancelling_child_run() -> None:
    planner = Agent(
        AgentDefinition(
            id="direct-cancel-planner",
            version="1.0.0",
            instructions="Plan.",
            response_model=PlannerDecision,
        ),
        model=FakeModel(
            [
                _json_response(
                    {
                        "action": "create",
                        "goal": "Wait for direct cancellation",
                        "steps": [{"description": "Block"}],
                        "reason": "One blocking Step",
                        "final_output": None,
                    }
                )
            ]
        ),
    )
    executor_model = GatedStructuredModel()
    executor = Agent(
        AgentDefinition(
            id="direct-cancel-executor",
            version="1.0.0",
            instructions="Execute.",
            response_model=StepOutcome,
        ),
        model=executor_model,
    )
    flow = Flow.planner_react(
        id="direct-cancel-flow",
        planner=planner,
        executor=executor,
    )
    observed: list[Event] = []
    task = asyncio.create_task(flow.run("Wait", on_event=observed.append))
    await asyncio.wait_for(executor_model.requested.wait(), timeout=1)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    step_started = next(event for event in observed if event.kind == "step.started")
    child_events = await executor.event_store.list(step_started.payload["agent_run_id"])
    assert child_events[-1].kind == "run.cancelled"
    flow_events = await flow.event_store.list(observed[0].run_id)
    assert "flow.succeeded" not in [event.kind for event in flow_events]
    assert "flow.failed" not in [event.kind for event in flow_events]


def test_planner_react_requires_structured_role_contracts() -> None:
    unstructured = Agent(
        AgentDefinition(
            id="unstructured",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("answer"),
    )
    planner = Agent(
        AgentDefinition(
            id="structured-planner",
            version="1.0.0",
            instructions="Plan.",
            response_model=PlannerDecision,
        ),
        model=FakeModel([]),
    )
    executor = Agent(
        AgentDefinition(
            id="structured-executor",
            version="1.0.0",
            instructions="Execute.",
            response_model=StepOutcome,
        ),
        model=FakeModel([]),
    )

    with pytest.raises(ValueError, match="Planner Agent response_model"):
        Flow.planner_react(id="bad-planner", planner=unstructured, executor=executor)
    with pytest.raises(ValueError, match="Executor Agent response_model"):
        Flow.planner_react(id="bad-executor", planner=planner, executor=unstructured)


def test_planning_response_models_reject_invalid_action_payloads() -> None:
    with pytest.raises(ValueError, match="create requires at least one Step"):
        PlannerDecision(
            action="create",
            goal="Missing Steps",
            steps=(),
            reason="Invalid for the test",
            final_output=None,
        )
    with pytest.raises(ValueError, match="unsuccessful Step requires"):
        StepOutcome(
            success=False,
            summary="Failed without an error",
            output=None,
            error=None,
            artifact_ids=(),
        )
    assert PlanStepDraft(description=" Execute one Step ").description == "Execute one Step"
