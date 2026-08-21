import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

from base_agent import (
    Agent,
    AgentDefinition,
    AwaitHumanConfirmations,
    CheckpointClaimError,
    CheckpointConflictError,
    CheckpointNotFoundError,
    Confirmation,
    Flow,
    FlowCheckpoint,
    FlowStatus,
    InMemoryCheckpointStore,
    InMemoryFlowRunStore,
    PlannerDecision,
    RunStatus,
    SideEffect,
    Stage,
    StepOutcome,
    ToolConfirmation,
    ToolConfirmationDecision,
    WaitForInput,
    tool,
)
from base_agent.events import InMemoryEventStore
from base_agent.flows import PlanStepStatus
from base_agent.models import ModelResponse, PendingInputKind, ToolCall
from base_agent.providers import ProviderCapabilities, ProviderInfo
from base_agent.server import create_http_app
from base_agent.testing import FakeModel


def _definition(*tools: str) -> AgentDefinition:
    return AgentDefinition(
        id="interactive-agent",
        version="1.0.0",
        instructions="Use the enabled tools.",
        tools=tools,
        max_model_turns=2,
        max_tool_calls_per_turn=4,
    )


def _flow_checkpoint_writer(store: InMemoryFlowRunStore):
    async def write(checkpoint: FlowCheckpoint) -> FlowCheckpoint:
        expected = checkpoint.revision - 1 or None
        return await store.save(checkpoint, expected_revision=expected)

    return write


async def test_tool_input_waits_and_resumes_the_same_run() -> None:
    @tool
    async def ask_region() -> WaitForInput:
        return WaitForInput("Which region?", {"field": "region"})

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="ask-1", name="ask_region", arguments={}),)
            ),
            ModelResponse(content="APAC report"),
        ]
    )
    agent = Agent(_definition("ask_region"), model=model, tools=(ask_region,))

    waiting = await agent.run("Build a report")

    assert waiting.status is RunStatus.WAITING
    assert waiting.pending_input is not None
    assert waiting.pending_input.kind is PendingInputKind.TOOL_INPUT
    assert waiting.pending_input.tool_call_id == "ask-1"
    assert waiting.pending_input.prompt == "Which region?"
    checkpoint = await agent.checkpoint_store.load(waiting.run_id)
    assert checkpoint.run_id == waiting.run_id

    completed = await agent.resume(waiting.run_id, "APAC")

    assert completed.status is RunStatus.SUCCEEDED
    assert completed.run_id == waiting.run_id
    assert completed.text == "APAC report"
    assert completed.tool_results[0].data == {"input": "APAC"}
    assert model.requests[1].messages[-1].tool_call_id == "ask-1"
    assert '"input": "APAC"' in model.requests[1].messages[-1].content
    with pytest.raises(CheckpointNotFoundError):
        await agent.checkpoint_store.load(waiting.run_id)


async def test_waiting_run_handle_can_be_cancelled_and_consumes_checkpoint() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?")

    agent = Agent(
        _definition("ask"),
        model=FakeModel(
            [ModelResponse(tool_calls=(ToolCall(id="ask-cancel", name="ask", arguments={}),))]
        ),
        tools=(ask,),
    )
    handle = agent.start("Begin")
    waiting = await handle.wait()
    assert waiting.status is RunStatus.WAITING

    cancelled = await handle.cancel()

    assert cancelled.status is RunStatus.CANCELLED
    assert handle.status is RunStatus.CANCELLED
    assert await handle.wait() is cancelled
    with pytest.raises(CheckpointNotFoundError):
        await agent.checkpoint_store.load(handle.run_id)


async def test_checkpoint_lease_rejects_concurrent_and_stale_owners() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?")

    store = InMemoryCheckpointStore()
    agent = Agent(
        _definition("ask"),
        model=FakeModel(
            [ModelResponse(tool_calls=(ToolCall(id="ask-lease", name="ask", arguments={}),))]
        ),
        tools=(ask,),
        checkpoint_store=store,
    )
    waiting = await agent.run("Begin")
    first = await store.claim(waiting.run_id, owner_id="worker-1", lease_seconds=0.01)

    with pytest.raises(CheckpointClaimError):
        await store.claim(waiting.run_id, owner_id="worker-2", lease_seconds=1)

    await asyncio.sleep(0.02)
    second = await store.claim(waiting.run_id, owner_id="worker-2", lease_seconds=1)
    with pytest.raises(CheckpointConflictError):
        await store.release(first, first.checkpoint)
    await store.complete(second)


async def test_resume_heartbeats_its_checkpoint_lease() -> None:
    class BlockingResumeModel:
        def __init__(self) -> None:
            self.calls = 0
            self.resumed = asyncio.Event()
            self.release = asyncio.Event()

        @property
        def info(self) -> ProviderInfo:
            return ProviderInfo(
                name="heartbeat-model",
                default_model="heartbeat-model",
                capabilities=ProviderCapabilities(tools=True),
            )

        async def complete(self, request) -> ModelResponse:
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(
                    tool_calls=(ToolCall(id="ask-heartbeat", name="ask", arguments={}),)
                )
            self.resumed.set()
            await self.release.wait()
            return ModelResponse(content="done")

    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?")

    model = BlockingResumeModel()
    store = InMemoryCheckpointStore()
    agent = Agent(
        _definition("ask"),
        model=model,
        tools=(ask,),
        checkpoint_store=store,
        checkpoint_lease_seconds=0.03,
    )
    waiting = await agent.run("Begin")
    continuation = asyncio.create_task(agent.resume(waiting.run_id, "yes"))
    await model.resumed.wait()
    await asyncio.sleep(0.06)

    with pytest.raises(CheckpointClaimError):
        await store.claim(waiting.run_id, owner_id="worker-2", lease_seconds=1)

    model.release.set()
    assert (await continuation).status is RunStatus.SUCCEEDED


async def test_resume_continues_remaining_calls_in_the_same_tool_batch() -> None:
    executed: list[str] = []

    @tool
    async def record(value: str) -> str:
        executed.append(value)
        return value

    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?")

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="record-1", name="record", arguments={"value": "before"}),
                    ToolCall(id="ask-1", name="ask", arguments={}),
                    ToolCall(id="record-2", name="record", arguments={"value": "after"}),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(_definition("record", "ask"), model=model, tools=(record, ask))

    waiting = await agent.run("Run batch")
    assert executed == ["before"]

    completed = await agent.resume(waiting.run_id, "yes")

    assert completed.status is RunStatus.SUCCEEDED
    assert executed == ["before", "after"]
    assert [result.call.id for result in completed.tool_results] == [
        "record-1",
        "ask-1",
        "record-2",
    ]


async def test_required_confirmation_can_wait_for_an_exact_host_decision() -> None:
    executed: list[str] = []

    @tool(
        side_effect=SideEffect.MUTATING,
        confirmation=Confirmation.REQUIRED,
    )
    async def publish(value: str) -> str:
        executed.append(value)
        return "published"

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="publish-1", name="publish", arguments={"value": "report"}),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        _definition("publish"),
        model=model,
        tools=(publish,),
        confirmation_policy=AwaitHumanConfirmations(),
    )

    waiting = await agent.run("Publish")

    assert waiting.status is RunStatus.WAITING
    assert executed == []
    assert waiting.pending_input is not None
    assert waiting.pending_input.kind is PendingInputKind.TOOL_CONFIRMATION

    with pytest.raises(ValueError, match="another request"):
        await agent.confirm(
            waiting.run_id,
            ToolConfirmation(
                request_id="wrong-request",
                decision=ToolConfirmationDecision.APPROVE,
                subject_id="user-1",
                reason_code="approved",
            ),
        )

    completed = await agent.confirm(
        waiting.run_id,
        ToolConfirmation(
            request_id=waiting.pending_input.request_id,
            decision=ToolConfirmationDecision.APPROVE,
            subject_id="user-1",
            reason_code="approved",
        ),
    )

    assert completed.status is RunStatus.SUCCEEDED
    assert executed == ["report"]
    confirmation = next(
        event
        for event in completed.events
        if event.kind == "tool.confirmation" and event.payload["approved"] is True
    )
    assert confirmation.payload["subject_id"] == "user-1"


async def test_checkpoint_can_resume_from_a_recreated_agent() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?")

    checkpoints = InMemoryCheckpointStore()
    events = InMemoryEventStore()
    first = Agent(
        _definition("ask"),
        model=FakeModel(
            [ModelResponse(tool_calls=(ToolCall(id="ask-1", name="ask", arguments={}),))]
        ),
        tools=(ask,),
        checkpoint_store=checkpoints,
        event_store=events,
    )
    waiting = await first.run("Begin")

    recreated = Agent(
        _definition("ask"),
        model=FakeModel([ModelResponse(content="continued")]),
        tools=(ask,),
        checkpoint_store=checkpoints,
        event_store=events,
    )
    completed = await recreated.resume(waiting.run_id, "yes")
    replayed = tuple([event async for event in events.subscribe(waiting.run_id)])

    assert completed.status is RunStatus.SUCCEEDED
    assert replayed[-1].kind == "run.succeeded"
    assert "run.waiting" in {event.kind for event in replayed}
    assert [event.kind for event in completed.events].count("run.started") == 1
    assert "run.resumed" in {event.kind for event in completed.events}


async def test_http_recovers_waiting_status_and_resumes_after_server_restart() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Continue?", {"field": "approval"})

    checkpoints = InMemoryCheckpointStore()
    events = InMemoryEventStore()
    first = Agent(
        _definition("ask"),
        model=FakeModel(
            [ModelResponse(tool_calls=(ToolCall(id="ask-http", name="ask", arguments={}),))]
        ),
        tools=(ask,),
        checkpoint_store=checkpoints,
        event_store=events,
    )
    waiting = await first.run("Begin")
    recreated = Agent(
        _definition("ask"),
        model=FakeModel([ModelResponse(content="continued")]),
        tools=(ask,),
        checkpoint_store=checkpoints,
        event_store=events,
    )
    app = create_http_app(recreated, event_store=events)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        recovered = await client.get(f"/v1/runs/{waiting.run_id}")
        resumed = await client.post(
            f"/v1/runs/{waiting.run_id}/resume",
            json={"input": "yes"},
        )

    assert recovered.status_code == 200
    assert recovered.json()["status"] == "waiting"
    assert recovered.json()["pending_input"] == {
        "request_id": waiting.pending_input.request_id,
        "kind": "tool_input",
        "tool_call_id": "ask-http",
        "tool_name": "ask",
        "prompt": "Continue?",
        "metadata": {"field": "approval"},
    }
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "succeeded"
    assert resumed.json()["text"] == "continued"


async def test_sequential_flow_propagates_and_resumes_child_waiting() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Region?")

    query = Agent(
        _definition("ask"),
        model=FakeModel(
            [
                ModelResponse(tool_calls=(ToolCall(id="ask-flow", name="ask", arguments={}),)),
                ModelResponse(content="query ready"),
            ]
        ),
        tools=(ask,),
    )
    report = Agent(
        AgentDefinition(
            id="report-agent",
            version="1.0.0",
            instructions="Report.",
        ),
        model=FakeModel([ModelResponse(content="report ready")]),
    )
    flow = Flow.sequence(
        id="interactive-flow",
        stages=(
            Stage(name="query", agent=query),
            Stage(name="report", agent=report, input_from="query"),
        ),
    )

    waiting = await flow.run("Build")

    assert waiting.status is FlowStatus.WAITING
    assert waiting.pending_input is not None
    assert waiting.pending_input.tool_call_id == "ask-flow"
    assert waiting.waiting_child_run_id is not None
    assert [event.kind for event in waiting.events][-2:] == [
        "stage.waiting",
        "flow.waiting",
    ]

    completed = await flow.resume(waiting.flow_run_id, "APAC")

    assert completed.status is FlowStatus.SUCCEEDED
    assert [stage.name for stage in completed.stages] == ["query", "report"]
    assert completed.output == "report ready"
    assert "flow.resumed" in {event.kind for event in completed.events}


async def test_planner_react_flow_resumes_a_waiting_executor() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Region?")

    planner = Agent(
        AgentDefinition(
            id="interactive-planner",
            version="1.0.0",
            instructions="Plan.",
            response_model=PlannerDecision,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    content=json.dumps(
                        {
                            "action": "create",
                            "goal": "Build report",
                            "steps": [{"description": "Analyze region"}],
                            "reason": "one step",
                            "final_output": None,
                        }
                    )
                ),
                ModelResponse(
                    content=json.dumps(
                        {
                            "action": "complete",
                            "goal": "Build report",
                            "steps": [],
                            "reason": "done",
                            "final_output": "report ready",
                        }
                    )
                ),
            ]
        ),
    )
    executor = Agent(
        AgentDefinition(
            id="interactive-executor",
            version="1.0.0",
            instructions="Execute.",
            tools=("ask",),
            max_model_turns=2,
            response_model=StepOutcome,
        ),
        model=FakeModel(
            [
                ModelResponse(tool_calls=(ToolCall(id="ask-plan", name="ask", arguments={}),)),
                ModelResponse(
                    content=json.dumps(
                        {
                            "success": True,
                            "summary": "analyzed",
                            "output": "APAC",
                            "error": None,
                            "artifact_ids": [],
                        }
                    )
                ),
            ]
        ),
        tools=(ask,),
    )
    flow = Flow.planner_react(
        id="interactive-plan",
        planner=planner,
        executor=executor,
        forward_child_events=True,
    )

    waiting = await flow.run("Build")

    assert waiting.status is FlowStatus.WAITING
    assert waiting.pending_input is not None
    assert waiting.pending_input.tool_call_id == "ask-plan"

    completed = await flow.resume(waiting.flow_run_id, "APAC")

    assert completed.status is FlowStatus.SUCCEEDED
    assert completed.output == "report ready"
    assert "child.run.resumed" in {event.kind for event in completed.events}
    assert [child.run.status for child in completed.child_runs] == [
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
    ]


async def test_planner_react_flow_can_wait_again_after_resume() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("More information?")

    planner = Agent(
        AgentDefinition(
            id="repeated-wait-planner",
            version="1.0.0",
            instructions="Plan.",
            response_model=PlannerDecision,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    content=json.dumps(
                        {
                            "action": "create",
                            "goal": "Build report",
                            "steps": [{"description": "Collect report inputs"}],
                            "reason": "one step",
                            "final_output": None,
                        }
                    )
                ),
                ModelResponse(
                    content=json.dumps(
                        {
                            "action": "complete",
                            "goal": "Build report",
                            "steps": [],
                            "reason": "done",
                            "final_output": "report ready",
                        }
                    )
                ),
            ]
        ),
    )
    executor = Agent(
        AgentDefinition(
            id="repeated-wait-executor",
            version="1.0.0",
            instructions="Execute.",
            tools=("ask",),
            max_model_turns=3,
            response_model=StepOutcome,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(ToolCall(id="ask-region", name="ask", arguments={}),)
                ),
                ModelResponse(
                    tool_calls=(ToolCall(id="ask-period", name="ask", arguments={}),)
                ),
                ModelResponse(
                    content=json.dumps(
                        {
                            "success": True,
                            "summary": "inputs collected",
                            "output": "APAC 2025",
                            "error": None,
                            "artifact_ids": [],
                        }
                    )
                ),
            ]
        ),
        tools=(ask,),
    )
    flow = Flow.planner_react(
        id="repeated-wait-plan",
        planner=planner,
        executor=executor,
        forward_child_events=True,
    )

    first_wait = await flow.run("Build")
    second_wait = await flow.resume(first_wait.flow_run_id, "APAC")

    assert second_wait.status is FlowStatus.WAITING
    assert second_wait.pending_input is not None
    assert second_wait.pending_input.tool_call_id == "ask-period"
    assert second_wait.waiting_child_run_id == first_wait.waiting_child_run_id
    assert second_wait.plan is not None
    assert second_wait.plan.steps[0].status is PlanStepStatus.RUNNING
    second_wait_event_kinds = [event.kind for event in second_wait.events]
    assert "child.run.waiting" in second_wait_event_kinds
    assert second_wait_event_kinds[-2:] == ["flow.resumed", "flow.waiting"]

    completed = await flow.resume(second_wait.flow_run_id, "2025")

    assert completed.status is FlowStatus.SUCCEEDED
    assert completed.output == "report ready"
    assert [child.run.status for child in completed.child_runs] == [
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
    ]


async def test_sequential_flow_resumes_from_a_recreated_flow_checkpoint() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Region?")

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="ask-restored-flow", name="ask", arguments={}),)
            ),
            ModelResponse(content="query ready"),
        ]
    )
    agent_checkpoints = InMemoryCheckpointStore()
    flow_checkpoints = InMemoryFlowRunStore()

    def build_flow():
        query = Agent(
            _definition("ask"),
            model=model,
            tools=(ask,),
            checkpoint_store=agent_checkpoints,
        )
        report = Agent(
            AgentDefinition(
                id="restored-report",
                version="1.0.0",
                instructions="Report.",
            ),
            model=FakeModel.text("report ready"),
        )
        return Flow.sequence(
            id="restored-sequential",
            stages=(
                Stage(name="query", agent=query),
                Stage(name="report", agent=report, input_from="query"),
            ),
            checkpoint_writer=_flow_checkpoint_writer(flow_checkpoints),
        )

    waiting = await build_flow().run("Build")
    checkpoint = await flow_checkpoints.load(waiting.flow_run_id)
    recreated = build_flow()
    await recreated.restore_checkpoint(checkpoint)
    completed = await recreated.resume(waiting.flow_run_id, "APAC")

    assert completed.status is FlowStatus.SUCCEEDED
    assert completed.output == "report ready"
    assert [stage.name for stage in completed.stages] == ["query", "report"]


async def test_planner_react_resumes_from_a_recreated_flow_checkpoint() -> None:
    @tool
    async def ask() -> WaitForInput:
        return WaitForInput("Region?")

    planner_model = FakeModel(
        [
            ModelResponse(
                content=json.dumps(
                    {
                        "action": "create",
                        "goal": "Build report",
                        "steps": [{"description": "Analyze region"}],
                        "reason": "one step",
                        "final_output": None,
                    }
                )
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "action": "complete",
                        "goal": "Build report",
                        "steps": [],
                        "reason": "done",
                        "final_output": "report ready",
                    }
                )
            ),
        ]
    )
    executor_model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="ask-restored-plan", name="ask", arguments={}),)
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "success": True,
                        "summary": "analyzed",
                        "output": "APAC",
                        "error": None,
                        "artifact_ids": [],
                    }
                )
            ),
        ]
    )
    agent_checkpoints = InMemoryCheckpointStore()
    flow_checkpoints = InMemoryFlowRunStore()

    def build_flow():
        planner = Agent(
            AgentDefinition(
                id="restored-planner",
                version="1.0.0",
                instructions="Plan.",
                response_model=PlannerDecision,
            ),
            model=planner_model,
            checkpoint_store=agent_checkpoints,
        )
        executor = Agent(
            AgentDefinition(
                id="restored-executor",
                version="1.0.0",
                instructions="Execute.",
                tools=("ask",),
                max_model_turns=2,
                response_model=StepOutcome,
            ),
            model=executor_model,
            tools=(ask,),
            checkpoint_store=agent_checkpoints,
        )
        return Flow.planner_react(
            id="restored-plan-react",
            planner=planner,
            executor=executor,
            checkpoint_writer=_flow_checkpoint_writer(flow_checkpoints),
        )

    waiting = await build_flow().run("Build")
    checkpoint = await flow_checkpoints.load(waiting.flow_run_id)
    recreated = build_flow()
    await recreated.restore_checkpoint(checkpoint)
    completed = await recreated.resume(waiting.flow_run_id, "APAC")

    assert completed.status is FlowStatus.SUCCEEDED
    assert completed.output == "report ready"
    assert [child.run.status for child in completed.child_runs] == [
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
        RunStatus.SUCCEEDED,
    ]
