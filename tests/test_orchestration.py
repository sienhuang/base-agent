from __future__ import annotations

from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    AgentRuntime,
    EventType,
    ExecutionPlan,
    ExecutionState,
    InvalidPlanTransitionError,
    ModelToolStrategy,
    OrchestrationStrategy,
    PendingInput,
    PlanStatus,
    PlanStep,
    RuntimeCheckpoint,
    RuntimeContext,
    RuntimeServices,
    StepStatus,
    update_execution_plan,
)
from base_agent.testing import FakeModel


def test_model_tool_strategy_satisfies_public_protocol() -> None:
    assert isinstance(ModelToolStrategy(), OrchestrationStrategy)


class ImmediateStrategy:
    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        context.output = f"custom:{services.provider.name}"
        context.state_machine.transition_to(ExecutionState.COMPLETED)


@pytest.mark.asyncio
async def test_runtime_accepts_a_strategy_without_calling_the_default_model_loop() -> None:
    model = FakeModel([])
    agent = Agent(
        profile=AgentProfile(id="custom", instructions="Custom orchestration."),
        model=model,
        runtime=AgentRuntime(strategy=ImmediateStrategy()),
    )

    result = await agent.run("work")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "custom:fake-model"
    assert model.requests == ()


def test_execution_plan_resolves_dependencies_and_returns_new_revisions() -> None:
    original = ExecutionPlan(
        id="report",
        title="Build report",
        steps=(
            PlanStep(id="inspect", description="Inspect data"),
            PlanStep(
                id="publish",
                description="Publish result",
                dependencies=("inspect",),
            ),
        ),
    )

    inspecting = original.start_step("inspect")
    inspected = inspecting.complete_step("inspect", {"rows": 10})
    publishing = inspected.start_step("publish")
    completed = publishing.complete_step("publish")

    assert original.status is PlanStatus.CREATED
    assert original.steps[0].status is StepStatus.PENDING
    assert inspected.ready_steps()[0].id == "publish"
    assert completed.status is PlanStatus.COMPLETED
    assert completed.revision == 5


def test_execution_plan_validates_graph_and_transitions() -> None:
    with pytest.raises(ValidationError, match="unknown dependencies"):
        ExecutionPlan(
            id="invalid",
            title="Invalid",
            steps=(
                PlanStep(id="one", description="One", dependencies=("missing",)),
            ),
        )
    with pytest.raises(ValidationError, match="cycle"):
        ExecutionPlan(
            id="cyclic",
            title="Cyclic",
            steps=(
                PlanStep(id="one", description="One", dependencies=("two",)),
                PlanStep(id="two", description="Two", dependencies=("one",)),
            ),
        )

    plan = ExecutionPlan(
        id="ordered",
        title="Ordered",
        steps=(
            PlanStep(id="one", description="One"),
            PlanStep(id="two", description="Two", dependencies=("one",)),
        ),
    )
    with pytest.raises(InvalidPlanTransitionError, match="not ready"):
        plan.start_step("two")


class PlanningStrategy:
    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        plan = cast(ExecutionPlan, context.plan)
        if plan.status is PlanStatus.CREATED:
            await update_execution_plan(context, services, plan.start_step("work"))
            return
        running = cast(ExecutionPlan, context.plan)
        await update_execution_plan(
            context, services, running.complete_step("work", "done")
        )
        context.output = "done"
        context.state_machine.transition_to(ExecutionState.COMPLETED)


@pytest.mark.asyncio
async def test_plan_is_persisted_and_step_lifecycle_is_replayable() -> None:
    plan = ExecutionPlan(
        id="single",
        title="Single step",
        steps=(PlanStep(id="work", description="Do work"),),
    )
    agent = Agent(
        profile=AgentProfile(id="planner", instructions="Plan."),
        model=FakeModel([]),
        runtime=AgentRuntime(strategy=PlanningStrategy()),
    )

    result = await agent.run("work", plan=plan)
    run_id = UUID(str(result.metadata["run_id"]))
    run = await agent.get_run(run_id)
    events = await agent.events(run_id)

    assert result.metadata["plan"]["status"] == "completed"
    assert run.metadata["plan"]["steps"][0]["result"] == "done"
    assert [event.type for event in events] == [
        EventType.RUN_CREATED,
        EventType.PLAN_CREATED,
        EventType.RUN_STARTED,
        EventType.PLAN_UPDATED,
        EventType.STEP_STARTED,
        EventType.PLAN_UPDATED,
        EventType.STEP_COMPLETED,
        EventType.RUN_COMPLETED,
    ]


def test_plan_round_trips_through_a_waiting_checkpoint() -> None:
    plan = ExecutionPlan(
        id="resumable",
        title="Resumable",
        steps=(PlanStep(id="work", description="Work"),),
    ).start_step("work")
    runtime = AgentRuntime()
    context = runtime.create_context(
        AgentProfile(id="checkpoint", instructions="Wait."),
        "work",
        plan=plan,
    )
    context.state_machine.transition_to(ExecutionState.RUNNING)
    context.state_machine.transition_to(ExecutionState.WAITING)
    context.pending_input = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Continue?",
    )

    checkpoint = RuntimeCheckpoint.from_context(context)
    restored = RuntimeCheckpoint.model_validate_json(
        checkpoint.model_dump_json()
    ).restore()

    assert restored.plan == plan
    assert restored.plan is not None
    assert restored.plan.steps[0].status is StepStatus.RUNNING
