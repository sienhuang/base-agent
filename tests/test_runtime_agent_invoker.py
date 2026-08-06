import asyncio
from collections.abc import Sequence
from uuid import uuid4

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    AgentHandoff,
    AgentInvocationInput,
    AgentInvoker,
    AgentRuntimeInvoker,
    FlowAgent,
    FlowDefinition,
    FlowInput,
    FlowLifecycle,
    InMemoryArtifactStore,
    InMemoryCheckpointStore,
    InMemoryEventStore,
    InMemoryFlowRepository,
    InMemoryRunStore,
    ModelResponse,
    PendingInput,
    RunStatus,
    SequentialFlowStrategy,
    TokenUsage,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.stores import CheckpointNotFoundError
from base_agent.testing import FakeModel


@tool
async def ask_user(question: str) -> WaitForInput:
    """Ask for a required input."""
    return WaitForInput(prompt=question)


def make_definition(
    *,
    researcher_tools: tuple[str, ...] = (),
) -> FlowDefinition:
    return FlowDefinition(
        id="research-report",
        version="1.0.0",
        agents=(
            FlowAgent(
                key="researcher",
                definition=AgentDefinition(
                    id="research-agent",
                    version="1.0.0",
                    instructions="Research.",
                    tools=researcher_tools,
                    permissions=(
                        frozenset({"interaction:ask"})
                        if researcher_tools
                        else frozenset()
                    ),
                ),
            ),
            FlowAgent(
                key="writer",
                definition=AgentDefinition(
                    id="writer-agent",
                    version="1.0.0",
                    instructions="Write.",
                ),
            ),
        ),
        strategy="sequential",
    )


def make_agents(
    definition: FlowDefinition,
    *,
    researcher_responses: Sequence[ModelResponse],
    writer_responses: Sequence[ModelResponse],
    researcher_tools: tuple[object, ...] = (),
) -> tuple[dict[str, Agent], FakeModel, FakeModel]:
    run_store = InMemoryRunStore()
    event_store = InMemoryEventStore()
    checkpoint_store = InMemoryCheckpointStore()
    artifact_store = InMemoryArtifactStore()
    researcher_model = FakeModel(researcher_responses, name="research-model")
    writer_model = FakeModel(writer_responses, name="writer-model")
    agents = {
        "researcher": Agent(
            definition=definition.agent("researcher"),
            model=researcher_model,
            tools=researcher_tools,
            run_store=run_store,
            event_store=event_store,
            checkpoint_store=checkpoint_store,
            artifact_store=artifact_store,
        ),
        "writer": Agent(
            definition=definition.agent("writer"),
            model=writer_model,
            run_store=run_store,
            event_store=event_store,
            checkpoint_store=checkpoint_store,
            artifact_store=artifact_store,
        ),
    }
    return agents, researcher_model, writer_model


@pytest.mark.asyncio
async def test_runtime_invoker_executes_linked_agent_runs_and_handoff() -> None:
    definition = make_definition()
    agents, _, writer_model = make_agents(
        definition,
        researcher_responses=(
            ModelResponse(
                content="Research result.",
                usage=TokenUsage(input_tokens=5, output_tokens=2),
            ),
        ),
        writer_responses=(
            ModelResponse(
                content="Final report.",
                usage=TokenUsage(input_tokens=4, output_tokens=3),
            ),
        ),
    )
    invoker = AgentRuntimeInvoker(definition, agents)
    assert isinstance(invoker, AgentInvoker)
    flow_repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(flow_repository),
        invoker=invoker,
    )

    result = await strategy.run(
        definition,
        FlowInput(prompt="Research harness engineering."),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.output == "Final report."
    assert result.usage == TokenUsage(input_tokens=9, output_tokens=5)
    state = await flow_repository.get(result.run_id)
    for invocation in state.invocations:
        agent = agents[invocation.agent_key]
        child_run = await agent.get_run(invocation.id)
        assert child_run.metadata["execution_scope"] == "flow_invocation"
        assert child_run.metadata["flow_run_id"] == str(result.run_id)
        assert child_run.metadata["invocation_id"] == str(invocation.id)
        assert child_run.metadata["agent_key"] == invocation.agent_key
        assert (
            child_run.metadata["definition_fingerprint"]
            == invocation.definition_fingerprint
        )
        created_event = (await agent.events(invocation.id))[0]
        assert created_event.data["flow_run_id"] == str(result.run_id)
        assert created_event.data["agent_key"] == invocation.agent_key
    writer_prompt = writer_model.requests[0].messages[-1].content or ""
    assert "## Explicit Agent handoff" in writer_prompt
    assert "Research result." in writer_prompt
    assert '"source_agent_key":"researcher"' in writer_prompt


@pytest.mark.asyncio
async def test_runtime_invoker_resumes_same_waiting_agent_execution() -> None:
    definition = make_definition(researcher_tools=("ask_user",)).model_copy(
        update={"agents": make_definition(researcher_tools=("ask_user",)).agents[:1]}
    )
    agents, _, _ = make_agents(
        make_definition(researcher_tools=("ask_user",)),
        researcher_responses=(
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="ask_user",
                        arguments={"question": "Which region?"},
                    ),
                ),
                usage=TokenUsage(input_tokens=3, output_tokens=1),
            ),
            ModelResponse(
                content="APAC research.",
                usage=TokenUsage(input_tokens=2, output_tokens=1),
            ),
        ),
        writer_responses=(ModelResponse(content="unused"),),
        researcher_tools=(ask_user,),
    )
    researcher = agents["researcher"]
    invoker = AgentRuntimeInvoker(
        definition,
        {"researcher": researcher},
    )
    flow_repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(flow_repository),
        invoker=invoker,
    )

    waiting = await strategy.run(
        definition,
        FlowInput(prompt="Research the market."),
    )

    assert waiting.status is RunStatus.WAITING
    assert waiting.pending_input == PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Which region?",
    )
    assert waiting.waiting_invocation_id is not None
    child_run = await researcher.get_run(waiting.waiting_invocation_id)
    assert child_run.status is RunStatus.WAITING

    completed = await strategy.resume(definition, waiting.run_id, "APAC")

    assert completed.status is RunStatus.COMPLETED
    assert completed.output == "APAC research."
    assert completed.usage == TokenUsage(input_tokens=5, output_tokens=2)
    child_run = await researcher.get_run(waiting.waiting_invocation_id)
    assert child_run.status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_runtime_invoker_flow_cancel_clears_waiting_child_checkpoint() -> None:
    full_definition = make_definition(researcher_tools=("ask_user",))
    definition = full_definition.model_copy(
        update={"agents": full_definition.agents[:1]}
    )
    agents, _, _ = make_agents(
        full_definition,
        researcher_responses=(
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="ask_user",
                        arguments={"question": "Continue?"},
                    ),
                ),
            ),
        ),
        writer_responses=(ModelResponse(content="unused"),),
        researcher_tools=(ask_user,),
    )
    researcher = agents["researcher"]
    flow_repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(flow_repository),
        invoker=AgentRuntimeInvoker(
            definition,
            {"researcher": researcher},
        ),
    )
    waiting = await strategy.run(definition, FlowInput(prompt="Research."))
    assert waiting.waiting_invocation_id is not None

    cancelled = await strategy.cancel(waiting.run_id)

    assert cancelled.status is RunStatus.CANCELLED
    child = await researcher.get_run(waiting.waiting_invocation_id)
    assert child.status is RunStatus.CANCELLED
    with pytest.raises(CheckpointNotFoundError):
        await researcher.checkpoint_store.load(waiting.waiting_invocation_id)


@pytest.mark.asyncio
async def test_runtime_invoker_propagates_running_flow_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    @tool
    async def controlled(value: str) -> str:
        """Wait until the test releases the running Tool."""
        started.set()
        await release.wait()
        return value

    agent_definition = AgentDefinition(
        id="controlled-agent",
        version="1.0.0",
        instructions="Work.",
        tools=("controlled",),
    )
    definition = FlowDefinition(
        id="controlled-flow",
        version="1.0.0",
        agents=(FlowAgent(key="worker", definition=agent_definition),),
        strategy="sequential",
    )
    run_store = InMemoryRunStore()
    event_store = InMemoryEventStore()
    checkpoint_store = InMemoryCheckpointStore()
    artifact_store = InMemoryArtifactStore()
    agent = Agent(
        definition=agent_definition,
        model=FakeModel(
            (
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="controlled",
                            arguments={"value": "work"},
                        ),
                    ),
                ),
                ModelResponse(content="must not run"),
            )
        ),
        tools=(controlled,),
        run_store=run_store,
        event_store=event_store,
        checkpoint_store=checkpoint_store,
        artifact_store=artifact_store,
    )
    flow_repository = InMemoryFlowRepository()
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(flow_repository),
        invoker=AgentRuntimeInvoker(definition, {"worker": agent}),
    )
    flow_run_id = uuid4()
    task = asyncio.create_task(
        strategy.run(
            definition,
            FlowInput(prompt="Work."),
            run_id=flow_run_id,
        )
    )
    await started.wait()

    cancelled = await strategy.cancel(flow_run_id)
    release.set()
    late_result = await task

    assert cancelled.status is RunStatus.CANCELLED
    assert late_result.status is RunStatus.CANCELLED
    invocation = (await flow_repository.get(flow_run_id)).invocations[0]
    child = await agent.get_run(invocation.id)
    assert child.status is RunStatus.CANCELLED
    assert child.cancel_requested is True


def test_runtime_invoker_rejects_definition_mismatch() -> None:
    definition = make_definition()
    agents, _, _ = make_agents(
        definition,
        researcher_responses=(ModelResponse(content="research"),),
        writer_responses=(ModelResponse(content="write"),),
    )
    changed_researcher = Agent(
        definition=definition.agent("researcher").model_copy(
            update={"version": "2.0.0"}
        ),
        model=FakeModel((ModelResponse(content="changed"),)),
        run_store=agents["writer"].run_store,
        event_store=agents["writer"].event_store,
        checkpoint_store=agents["writer"].checkpoint_store,
        artifact_store=agents["writer"].artifact_store,
    )

    with pytest.raises(ValueError, match="definition does not match"):
        AgentRuntimeInvoker(
            definition,
            {**agents, "researcher": changed_researcher},
        )

def test_default_prompt_builder_enforces_handoff_size_limit() -> None:
    from base_agent import DefaultAgentInvocationPromptBuilder

    builder = DefaultAgentInvocationPromptBuilder(max_handoff_chars=10)
    invocation_input = AgentInvocationInput(
        prompt="Write.",
        handoff=AgentHandoff(
            source_agent_key="researcher",
            summary="This handoff is intentionally too large.",
        ),
    )

    with pytest.raises(ValueError, match="handoff exceeds"):
        builder.build(invocation_input)
