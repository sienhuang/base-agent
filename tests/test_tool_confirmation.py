import json
from uuid import UUID, uuid4

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    AgentResultStatus,
    AgentRuntimeInvoker,
    EventType,
    FlowAgent,
    FlowDefinition,
    FlowInput,
    FlowLifecycle,
    FlowSideEffectPhase,
    FlowToolSideEffectRecorder,
    InMemoryFlowRepository,
    InMemoryFlowSideEffectLedger,
    ModelResponse,
    RunStatus,
    SequentialFlowStrategy,
    ToolCall,
    ToolConfirmation,
    ToolConfirmationDecision,
    ToolConfirmationMode,
    ToolContext,
    ToolSideEffectMode,
    tool,
)
from base_agent.testing import FakeModel


def confirmation_from(result, decision: ToolConfirmationDecision) -> ToolConfirmation:
    pending = result.metadata["pending_input"]
    request = pending["metadata"]["request"]
    return ToolConfirmation(
        request_id=UUID(request["id"]),
        decision=decision,
        subject_id="operator-1",
        reason_code=(
            "change_approved"
            if decision is ToolConfirmationDecision.APPROVE
            else "change_rejected"
        ),
    )


@pytest.mark.asyncio
async def test_required_confirmation_approves_original_tool_call_once() -> None:
    executions: list[tuple[int, str]] = []

    @tool(
        side_effect=ToolSideEffectMode.IDEMPOTENT,
        confirmation=ToolConfirmationMode.REQUIRED,
    )
    async def charge(amount: int, context: ToolContext) -> str:
        assert context.confirmation is not None
        assert context.confirmation.subject_id == "operator-1"
        assert context.idempotency_key is not None
        executions.append((amount, context.idempotency_key))
        return "charged"

    definition = AgentDefinition(
        id="confirmed-payment-agent",
        version="1.0.0",
        instructions="Charge only after approval.",
        tools=("charge",),
    )
    ledger = InMemoryFlowSideEffectLedger()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="charge-call",
                        name="charge",
                        arguments={"amount": 42},
                    ),
                )
            ),
            ModelResponse(content="charged"),
        ]
    )
    agent = Agent(
        definition=definition,
        model=model,
        tools=(charge,),
        side_effect_recorder=FlowToolSideEffectRecorder(ledger),
    )
    flow_run_id = uuid4()
    invocation_id = uuid4()

    waiting = await agent.execute_invocation(
        "charge",
        flow_run_id=flow_run_id,
        invocation_id=invocation_id,
        agent_key="payment",
    )

    assert waiting.status is AgentResultStatus.WAITING
    assert waiting.metadata["pending_input"]["metadata"]["kind"] == (
        "tool_confirmation"
    )
    assert executions == []
    assert await ledger.list_for_invocation(invocation_id) == ()
    with pytest.raises(ValueError, match=r"Agent\.confirm"):
        await agent.resume(invocation_id, "yes")
    assert (await agent.get_run(invocation_id)).status is RunStatus.WAITING

    completed = await agent.confirm(
        invocation_id,
        confirmation_from(waiting, ToolConfirmationDecision.APPROVE),
    )

    assert completed.status is AgentResultStatus.COMPLETED
    assert executions[0][0] == 42
    assert len(executions) == 1
    assert completed.metadata["tool_calls"] == 1
    effects = await ledger.list_for_invocation(invocation_id)
    assert len(effects) == 1
    assert effects[0].phase is FlowSideEffectPhase.CONFIRMED
    event_types = [event.type for event in await agent.events(invocation_id)]
    assert EventType.TOOL_CONFIRMATION_REQUESTED in event_types
    assert EventType.TOOL_CONFIRMATION_DECIDED in event_types
    decision_event = next(
        event
        for event in await agent.events(invocation_id)
        if event.type is EventType.TOOL_CONFIRMATION_DECIDED
    )
    assert decision_event.data["decision"] == "approve"
    assert "amount" not in decision_event.model_dump_json()


@pytest.mark.asyncio
async def test_rejected_confirmation_never_starts_tool_or_ledger() -> None:
    executed = False

    @tool(
        side_effect=ToolSideEffectMode.UNSAFE,
        confirmation=ToolConfirmationMode.REQUIRED,
    )
    async def delete_record(record_id: str) -> str:
        nonlocal executed
        executed = True
        return record_id

    definition = AgentDefinition(
        id="confirmed-delete-agent",
        version="1.0.0",
        instructions="Respect rejection.",
        tools=("delete_record",),
    )
    ledger = InMemoryFlowSideEffectLedger()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="delete-call",
                        name="delete_record",
                        arguments={"record_id": "record-1"},
                    ),
                )
            ),
            ModelResponse(content="not deleted"),
        ]
    )
    agent = Agent(
        definition=definition,
        model=model,
        tools=(delete_record,),
        side_effect_recorder=FlowToolSideEffectRecorder(ledger),
    )
    invocation_id = uuid4()
    waiting = await agent.execute_invocation(
        "delete",
        flow_run_id=uuid4(),
        invocation_id=invocation_id,
        agent_key="deleter",
    )

    completed = await agent.confirm(
        invocation_id,
        confirmation_from(waiting, ToolConfirmationDecision.REJECT),
    )

    assert completed.status is AgentResultStatus.COMPLETED
    assert not executed
    assert await ledger.list_for_invocation(invocation_id) == ()
    tool_message = model.requests[1].messages[-1]
    payload = json.loads(tool_message.content or "{}")
    assert payload["status"] == "denied"
    assert payload["error_code"] == "tool_confirmation_rejected"
    assert EventType.TOOL_FAILED in {
        event.type for event in await agent.events(invocation_id)
    }


@pytest.mark.asyncio
async def test_mismatched_confirmation_does_not_consume_checkpoint() -> None:
    @tool(
        side_effect=ToolSideEffectMode.UNSAFE,
        confirmation=ToolConfirmationMode.REQUIRED,
    )
    async def publish() -> str:
        return "published"

    definition = AgentDefinition(
        id="confirmed-publish-agent",
        version="1.0.0",
        instructions="Publish.",
        tools=("publish",),
    )
    agent = Agent(
        definition=definition,
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="publish-call",
                            name="publish",
                            arguments={},
                        ),
                    )
                ),
                ModelResponse(content="done"),
            ]
        ),
        tools=(publish,),
    )
    invocation_id = uuid4()
    waiting = await agent.execute_invocation(
        "publish",
        flow_run_id=uuid4(),
        invocation_id=invocation_id,
        agent_key="publisher",
    )
    mismatch = confirmation_from(
        waiting,
        ToolConfirmationDecision.APPROVE,
    ).model_copy(update={"request_id": uuid4()})

    with pytest.raises(ValueError, match="another request"):
        await agent.confirm(invocation_id, mismatch)

    checkpoint = await agent.checkpoint_store.load(invocation_id)
    assert checkpoint.pending_input.metadata["kind"] == "tool_confirmation"


def test_confirmation_declaration_requires_a_side_effect() -> None:
    with pytest.raises(ValueError, match="unsafe or idempotent"):

        @tool(confirmation=ToolConfirmationMode.REQUIRED)
        async def invalid_confirmation() -> str:
            return "invalid"


@pytest.mark.asyncio
async def test_sequential_flow_propagates_typed_tool_confirmation() -> None:
    executed = False

    @tool(
        side_effect=ToolSideEffectMode.UNSAFE,
        confirmation=ToolConfirmationMode.REQUIRED,
    )
    async def deploy() -> str:
        nonlocal executed
        executed = True
        return "deployed"

    agent_definition = AgentDefinition(
        id="confirmed-deploy-agent",
        version="1.0.0",
        instructions="Deploy after approval.",
        tools=("deploy",),
    )
    flow = FlowDefinition(
        id="confirmed-deploy-flow",
        version="1.0.0",
        agents=(FlowAgent(key="deployer", definition=agent_definition),),
        strategy="sequential",
    )
    agent = Agent(
        definition=agent_definition,
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="deploy-call",
                            name="deploy",
                            arguments={},
                        ),
                    )
                ),
                ModelResponse(content="deployed"),
            ]
        ),
        tools=(deploy,),
    )
    strategy = SequentialFlowStrategy(
        lifecycle=FlowLifecycle(InMemoryFlowRepository()),
        invoker=AgentRuntimeInvoker(flow, {"deployer": agent}),
    )

    waiting = await strategy.run(flow, FlowInput(prompt="deploy"))
    assert waiting.pending_input is not None
    request = waiting.pending_input.metadata["request"]
    decision = ToolConfirmation(
        request_id=UUID(request["id"]),
        decision=ToolConfirmationDecision.APPROVE,
        subject_id="release-operator",
        reason_code="release_approved",
    )

    completed = await strategy.confirm(flow, waiting.run_id, decision)

    assert completed.status is RunStatus.COMPLETED
    assert executed
    assert completed.tool_call_count == 1
