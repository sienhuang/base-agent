from __future__ import annotations

import asyncio
import json
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    AgentRuntime,
    EventType,
    ExecutionPlan,
    ModelRequest,
    ModelResponse,
    OrchestrationStrategy,
    PlanningStrategy,
    PlanStatus,
    PlanStep,
    RunStatus,
    StepStatus,
    TokenUsage,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.testing import FakeModel


def _plan() -> ExecutionPlan:
    return ExecutionPlan(
        id="report",
        title="Build report",
        steps=(
            PlanStep(id="inspect", description="Inspect source data"),
            PlanStep(
                id="publish",
                description="Publish the report",
                executor="model",
                dependencies=("inspect",),
            ),
        ),
    )


def test_planning_strategy_satisfies_public_protocol() -> None:
    assert isinstance(PlanningStrategy(), OrchestrationStrategy)


@pytest.mark.asyncio
async def test_default_runtime_executes_supplied_plan_and_summarizes() -> None:
    model = FakeModel(
        [
            ModelResponse(
                content="Inspected 10 rows.",
                usage=TokenUsage(input_tokens=10, output_tokens=2),
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "steps": [
                            {
                                "id": "publish-revised",
                                "description": "Publish the revised report",
                                "executor": "model",
                                "dependencies": ["inspect"],
                            }
                        ]
                    }
                ),
                usage=TokenUsage(input_tokens=15, output_tokens=3),
            ),
            ModelResponse(
                content="Published report.md.",
                usage=TokenUsage(input_tokens=20, output_tokens=3),
            ),
            ModelResponse(
                content=json.dumps({"steps": []}),
                usage=TokenUsage(input_tokens=25, output_tokens=2),
            ),
            ModelResponse(
                content="The report was built and published.",
                usage=TokenUsage(input_tokens=30, output_tokens=4),
            ),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="planner", instructions="Execute carefully."),
        model=model,
    )

    result = await agent.run("Build the report", plan=_plan())
    run_id = UUID(result.metadata["run_id"])
    stored = await agent.get_run(run_id)
    events = await agent.events(run_id)
    final_plan = ExecutionPlan.model_validate(result.metadata["plan"])

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "The report was built and published."
    assert result.usage == TokenUsage(input_tokens=100, output_tokens=14)
    assert final_plan.status is PlanStatus.COMPLETED
    assert [step.status for step in final_plan.steps] == [
        StepStatus.COMPLETED,
        StepStatus.COMPLETED,
    ]
    assert [step.id for step in final_plan.steps] == ["inspect", "publish-revised"]
    assert [step.result for step in final_plan.steps] == [
        "Inspected 10 rows.",
        "Published report.md.",
    ]
    assert final_plan.metadata["replan_count"] == 2
    assert stored.metadata["plan"] == result.metadata["plan"]
    assert len(model.requests) == 5
    assert "Current step [inspect]" in (model.requests[0].messages[-1].content or "")
    assert "Update the execution plan" in (
        model.requests[1].messages[-1].content or ""
    )
    assert "Current step [publish-revised]" in (
        model.requests[2].messages[-1].content or ""
    )
    assert "Update the execution plan" in (
        model.requests[3].messages[-1].content or ""
    )
    assert model.requests[1].tools == ()
    assert model.requests[3].tools == ()
    assert model.requests[4].tools == ()
    assert "Deliver the final result" in (model.requests[4].messages[-1].content or "")
    event_types = [event.type for event in events]
    assert event_types.count(EventType.STEP_STARTED) == 2
    assert event_types.count(EventType.STEP_COMPLETED) == 2
    assert event_types.count(EventType.PLAN_UPDATED) == 6
    assert event_types[-1] is EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_planning_flag_generates_and_executes_plan_in_same_run() -> None:
    generated = {
        "id": "generated-report",
        "title": "Generated report",
        "steps": [
            {
                "id": "build",
                "description": "Build the requested report",
                "executor": "model",
                "dependencies": [],
            }
        ],
    }
    model = FakeModel(
        [
            ModelResponse(
                content=json.dumps(generated),
                usage=TokenUsage(input_tokens=7, output_tokens=3),
            ),
            ModelResponse(
                content="Built report.md.",
                usage=TokenUsage(input_tokens=11, output_tokens=2),
            ),
            ModelResponse(
                content=json.dumps({"steps": []}),
                usage=TokenUsage(input_tokens=12, output_tokens=1),
            ),
            ModelResponse(
                content="The requested report is ready.",
                usage=TokenUsage(input_tokens=13, output_tokens=3),
            ),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="auto-planner", instructions="Work."),
        model=model,
    )

    result = await agent.run("Build a report", planning=True)
    final_plan = ExecutionPlan.model_validate(result.metadata["plan"])
    events = await agent.events(UUID(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "The requested report is ready."
    assert result.usage == TokenUsage(input_tokens=43, output_tokens=9)
    assert result.metadata["planning_requested"] is True
    assert result.metadata["model_calls"] == 4
    assert final_plan.status is PlanStatus.COMPLETED
    assert final_plan.metadata == {
        "generated": True,
        "provider": "fake-model",
        "replan_count": 1,
    }
    assert model.requests[0].tools == ()
    assert "Create a concise execution plan" in (
        model.requests[0].messages[-1].content or ""
    )
    assert [event.type for event in events].count(EventType.PLAN_CREATED) == 1
    assert [event.type for event in events].count(EventType.STEP_STARTED) == 1
    assert [event.type for event in events].count(EventType.STEP_COMPLETED) == 1


@pytest.mark.asyncio
async def test_invalid_generated_plan_fails_without_executing_tools() -> None:
    model = FakeModel([ModelResponse(content="not-json")])
    agent = Agent(
        profile=AgentProfile(id="bad-auto-planner", instructions="Work."),
        model=model,
    )

    result = await agent.run("Plan this", planning=True)

    assert result.status is AgentResultStatus.FAILED
    assert "invalid execution plan" in (result.error or "")
    assert result.metadata["plan"] is None
    assert len(model.requests) == 1


@tool
async def lookup(value: str) -> str:
    """Look up one value."""
    return f"found:{value}"


@pytest.mark.asyncio
async def test_react_plan_step_uses_multi_tool_action_batch() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="lookup-1", name="lookup", arguments={"value": "rows"}),
                    ToolCall(id="lookup-2", name="lookup", arguments={"value": "columns"}),
                )
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "success": True,
                        "result": "Looked up rows and columns.",
                        "attachments": ["lookup.json"],
                    }
                )
            ),
            ModelResponse(content=json.dumps({"steps": []})),
            ModelResponse(content="Lookup completed."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="tool-planner",
            instructions="Use tools.",
            tools=("lookup",),
        ),
        model=model,
        tools=(lookup,),
    )
    plan = ExecutionPlan(
        id="lookup-plan",
        title="Lookup",
        steps=(PlanStep(id="lookup", description="Look up rows", executor="react"),),
    )

    result = await agent.run("Find rows", plan=plan)
    events = await agent.events(UUID(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.COMPLETED
    assert result.metadata["steps"] == 4
    assert result.metadata["tool_calls"] == 2
    final_plan = ExecutionPlan.model_validate(result.metadata["plan"])
    assert final_plan.steps[0].success is True
    assert final_plan.steps[0].result == "Looked up rows and columns."
    assert final_plan.steps[0].attachments == ("lookup.json",)
    assert final_plan.steps[0].metadata["react_iterations"] == 2
    assert len(model.requests[1].messages[-2:]) == 2
    assert all(message.role.value == "tool" for message in model.requests[1].messages[-2:])
    assert [event.type for event in events].count(EventType.STEP_STARTED) == 1
    assert [event.type for event in events].count(EventType.STEP_COMPLETED) == 1
    event_types = [event.type for event in events]
    assert event_types.count(EventType.TOOL_COMPLETED) == 2
    assert event_types.count(EventType.REACT_ITERATION_STARTED) == 2
    assert event_types.count(EventType.REACT_ACTION_BATCH_SELECTED) == 1
    assert event_types.count(EventType.REACT_OBSERVATION_BATCH_RECORDED) == 1
    assert event_types.count(EventType.REACT_ITERATION_COMPLETED) == 2


@tool
async def ask_user(question: str) -> WaitForInput:
    """Request required user input."""
    return WaitForInput(prompt=question)


@pytest.mark.asyncio
async def test_waiting_plan_step_resumes_same_run_and_step() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={"question": "Which region?"},
                    ),
                )
            ),
            ModelResponse(content="Built the APAC report."),
            ModelResponse(content=json.dumps({"steps": []})),
            ModelResponse(content="The APAC report is ready."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="waiting-planner",
            instructions="Ask when needed.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )
    plan = ExecutionPlan(
        id="waiting-plan",
        title="Regional report",
        steps=(PlanStep(id="build", description="Build regional report"),),
    )

    waiting = await agent.run("Build report", plan=plan)
    run_id = UUID(waiting.metadata["run_id"])
    waiting_plan = ExecutionPlan.model_validate(waiting.metadata["plan"])
    checkpoint = await agent.checkpoint_store.load(run_id)

    assert waiting.status is AgentResultStatus.WAITING
    assert waiting_plan.status is PlanStatus.WAITING
    assert waiting_plan.steps[0].status is StepStatus.WAITING
    assert checkpoint.plan == waiting_plan

    completed = await agent.resume(run_id, "APAC")
    completed_plan = ExecutionPlan.model_validate(completed.metadata["plan"])
    event_types = [event.type for event in await agent.events(run_id)]

    assert completed.status is AgentResultStatus.COMPLETED
    assert completed_plan.status is PlanStatus.COMPLETED
    assert completed_plan.steps[0].result == "Built the APAC report."
    assert event_types.count(EventType.STEP_STARTED) == 1
    assert event_types.count(EventType.STEP_WAITING) == 1
    assert event_types.count(EventType.STEP_RESUMED) == 1
    assert event_types.count(EventType.STEP_COMPLETED) == 1


@pytest.mark.asyncio
async def test_react_action_batch_resumes_remaining_tools_without_repeating_model() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="ask-batch",
                        name="ask_user",
                        arguments={"question": "Which region?"},
                    ),
                    ToolCall(
                        id="lookup-batch",
                        name="lookup",
                        arguments={"value": "regional rows"},
                    ),
                )
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "success": True,
                        "result": "Built the APAC report from regional rows.",
                        "attachments": [],
                    }
                )
            ),
            ModelResponse(content=json.dumps({"steps": []})),
            ModelResponse(content="The APAC report is ready."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="waiting-react-planner",
            instructions="Ask and inspect.",
            tools=("ask_user", "lookup"),
        ),
        model=model,
        tools=(ask_user, lookup),
    )
    plan = ExecutionPlan(
        id="waiting-react-plan",
        title="Regional ReAct report",
        steps=(
            PlanStep(
                id="build",
                description="Build regional report",
                executor="react",
            ),
        ),
    )

    waiting = await agent.run("Build report", plan=plan)
    run_id = UUID(waiting.metadata["run_id"])
    assert waiting.status is AgentResultStatus.WAITING
    assert len(model.requests) == 1

    completed = await agent.resume(run_id, "APAC")
    final_plan = ExecutionPlan.model_validate(completed.metadata["plan"])
    events = await agent.events(run_id)
    event_types = [event.type for event in events]

    assert completed.status is AgentResultStatus.COMPLETED
    assert final_plan.steps[0].result == "Built the APAC report from regional rows."
    assert len(model.requests) == 4
    assert event_types.count(EventType.TOOL_STARTED) == 2
    assert event_types.count(EventType.TOOL_WAITING) == 1
    assert event_types.count(EventType.TOOL_COMPLETED) == 1
    assert event_types.count(EventType.REACT_ACTION_BATCH_SELECTED) == 1
    assert event_types.count(EventType.REACT_OBSERVATION_BATCH_RECORDED) == 1


@pytest.mark.asyncio
async def test_unknown_plan_executor_fails_before_model_or_tool_execution() -> None:
    model = FakeModel([])
    agent = Agent(
        profile=AgentProfile(id="invalid-planner", instructions="Work."),
        model=model,
    )
    plan = ExecutionPlan(
        id="invalid-executor",
        title="Invalid",
        steps=(PlanStep(id="work", description="Work", executor="remote-agent"),),
    )

    result = await agent.run("work", plan=plan)
    final_plan = ExecutionPlan.model_validate(result.metadata["plan"])
    stored = await agent.get_run(UUID(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.FAILED
    assert "unsupported plan step executors" in (result.error or "")
    assert final_plan.status is PlanStatus.FAILED
    assert stored.status is RunStatus.FAILED
    assert model.requests == ()


class BlockingPlanModel:
    name = "blocking-plan-model"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse(content="too late")


@pytest.mark.asyncio
async def test_cancellation_settles_active_plan_and_step() -> None:
    model = BlockingPlanModel()
    agent = Agent(
        profile=AgentProfile(id="cancel-planner", instructions="Work."),
        model=model,
    )
    plan = ExecutionPlan(
        id="cancel-plan",
        title="Cancel",
        steps=(PlanStep(id="work", description="Long work"),),
    )
    handle = await agent.start("work", plan=plan)
    await model.started.wait()

    await handle.cancel()
    model.release.set()
    result = await handle.result()
    final_plan = ExecutionPlan.model_validate(result.metadata["plan"])
    event_types = [event.type for event in await handle.events()]

    assert result.status is AgentResultStatus.CANCELLED
    assert final_plan.status is PlanStatus.CANCELLED
    assert final_plan.steps[0].status is StepStatus.CANCELLED
    assert EventType.STEP_CANCELLED in event_types


@pytest.mark.asyncio
async def test_planning_summary_can_be_disabled() -> None:
    model = FakeModel(
        [
            ModelResponse(content="step result"),
            ModelResponse(content=json.dumps({"steps": []})),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="no-summary", instructions="Work."),
        model=model,
        runtime=AgentRuntime(planning_strategy=PlanningStrategy(summarize=False)),
    )
    plan = ExecutionPlan(
        id="no-summary-plan",
        title="One step",
        steps=(PlanStep(id="work", description="Do work"),),
    )

    result = await agent.run("work", plan=plan)

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "One step\n\n- [work] Do work: step result"
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_invalid_replanned_steps_fail_without_changing_completed_history() -> None:
    model = FakeModel(
        [
            ModelResponse(content="step result"),
            ModelResponse(
                content=json.dumps(
                    {
                        "steps": [
                            {
                                "id": "next",
                                "description": "Invalid next step",
                                "dependencies": ["removed-pending-step"],
                            }
                        ]
                    }
                )
            ),
        ]
    )
    agent = Agent(
        profile=AgentProfile(id="invalid-replan", instructions="Work."),
        model=model,
    )
    plan = ExecutionPlan(
        id="invalid-replan-plan",
        title="Invalid replan",
        steps=(
            PlanStep(id="done-first", description="Complete the first step"),
            PlanStep(
                id="removed-pending-step",
                description="This pending step may be replaced",
                dependencies=("done-first",),
            ),
        ),
    )

    result = await agent.run("work", plan=plan)
    final_plan = ExecutionPlan.model_validate(result.metadata["plan"])

    assert result.status is AgentResultStatus.FAILED
    assert "invalid updated execution plan" in (result.error or "")
    assert final_plan.steps[0].id == "done-first"
    assert final_plan.steps[0].status is StepStatus.COMPLETED
    assert final_plan.steps[0].result == "step result"
