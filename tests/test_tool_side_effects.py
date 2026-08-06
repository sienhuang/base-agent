import hashlib
from uuid import uuid4

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    AgentResultStatus,
    FlowSideEffectPhase,
    FlowSideEffectRetryMode,
    FlowToolSideEffectRecorder,
    InMemoryFlowSideEffectLedger,
    ModelResponse,
    ToolCall,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    ToolResultStatus,
    ToolSideEffectMode,
    ToolSideEffectReplayUnsafeError,
    tool,
)
from base_agent.testing import FakeModel


def test_tool_side_effect_classification_is_explicit_and_backward_compatible() -> None:
    @tool
    async def legacy_lookup() -> str:
        return "ok"

    @tool(side_effect=ToolSideEffectMode.READ_ONLY)
    async def lookup() -> str:
        return "ok"

    assert legacy_lookup.side_effect_mode is ToolSideEffectMode.UNSPECIFIED
    assert lookup.side_effect_mode is ToolSideEffectMode.READ_ONLY

    with pytest.raises(ValueError, match="must accept ToolContext"):

        @tool(side_effect=ToolSideEffectMode.IDEMPOTENT)
        async def invalid_charge() -> str:
            return "charged"


@pytest.mark.asyncio
async def test_idempotent_flow_tool_records_only_metadata_and_receives_stable_key() -> None:
    received_keys: list[str] = []

    @tool(side_effect=ToolSideEffectMode.IDEMPOTENT)
    async def charge(amount: int, context: ToolContext) -> dict[str, int]:
        assert context.tool_call_id == "charge-call"
        assert context.idempotency_key is not None
        received_keys.append(context.idempotency_key)
        return {"amount": amount}

    definition = AgentDefinition(
        id="payment-agent",
        version="1.0.0",
        instructions="Charge once.",
        tools=("charge",),
    )
    ledger = InMemoryFlowSideEffectLedger()
    agent = Agent(
        definition=definition,
        model=FakeModel(
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
                ModelResponse(content="done"),
            ]
        ),
        tools=(charge,),
        side_effect_recorder=FlowToolSideEffectRecorder(ledger),
    )
    flow_run_id = uuid4()
    invocation_id = uuid4()

    result = await agent.execute_invocation(
        "charge",
        flow_run_id=flow_run_id,
        invocation_id=invocation_id,
        agent_key="payment",
    )

    assert result.status is AgentResultStatus.COMPLETED
    assert len(received_keys) == 1
    records = await ledger.list_for_invocation(invocation_id)
    assert len(records) == 1
    record = records[0]
    assert record.flow_run_id == flow_run_id
    assert record.operation_name == "charge"
    assert record.phase is FlowSideEffectPhase.CONFIRMED
    assert record.retry_mode is FlowSideEffectRetryMode.IDEMPOTENT
    assert record.idempotency_key_digest == hashlib.sha256(
        received_keys[0].encode()
    ).hexdigest()
    serialized = record.model_dump_json()
    assert received_keys[0] not in serialized
    assert "amount" not in serialized


@pytest.mark.asyncio
async def test_invalid_arguments_do_not_start_side_effect_evidence() -> None:
    executed = False

    @tool(side_effect=ToolSideEffectMode.UNSAFE)
    async def charge(amount: int) -> str:
        nonlocal executed
        executed = True
        return str(amount)

    definition = AgentDefinition(
        id="validated-payment-agent",
        version="1.0.0",
        instructions="Validate first.",
        tools=("charge",),
    )
    ledger = InMemoryFlowSideEffectLedger()
    invocation_id = uuid4()
    agent = Agent(
        definition=definition,
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="invalid-charge",
                            name="charge",
                            arguments={"amount": "not-an-integer"},
                        ),
                    )
                ),
                ModelResponse(content="handled"),
            ]
        ),
        tools=(charge,),
        side_effect_recorder=FlowToolSideEffectRecorder(ledger),
    )

    result = await agent.execute_invocation(
        "charge",
        flow_run_id=uuid4(),
        invocation_id=invocation_id,
        agent_key="payment",
    )

    assert result.status is AgentResultStatus.COMPLETED
    assert not executed
    assert await ledger.list_for_invocation(invocation_id) == ()


@pytest.mark.asyncio
async def test_unprotected_started_effect_is_denied_before_replay() -> None:
    ledger = InMemoryFlowSideEffectLedger()
    recorder = FlowToolSideEffectRecorder(ledger)
    flow_run_id = uuid4()
    invocation_id = uuid4()
    context = ToolContext(
        run_id=invocation_id,
        resources=None,  # type: ignore[arg-type]
        artifacts=None,  # type: ignore[arg-type]
        memories=None,  # type: ignore[arg-type]
        flow_run_id=flow_run_id,
        invocation_id=invocation_id,
    )
    call = ToolCall(id="unsafe-call", name="payments.charge", arguments={})

    await recorder.start(
        call,
        tool_name=call.name,
        mode=ToolSideEffectMode.UNSAFE,
        context=context,
    )

    with pytest.raises(ToolSideEffectReplayUnsafeError):
        await recorder.start(
            call,
            tool_name=call.name,
            mode=ToolSideEffectMode.UNSAFE,
            context=context,
        )


@pytest.mark.asyncio
async def test_executor_does_not_repeat_confirmed_unsafe_tool_call() -> None:
    execution_count = 0

    @tool(side_effect=ToolSideEffectMode.UNSAFE)
    async def charge() -> str:
        nonlocal execution_count
        execution_count += 1
        return "charged"

    ledger = InMemoryFlowSideEffectLedger()
    invocation_id = uuid4()
    context = ToolContext(
        run_id=invocation_id,
        resources=None,  # type: ignore[arg-type]
        artifacts=None,  # type: ignore[arg-type]
        memories=None,  # type: ignore[arg-type]
        flow_run_id=uuid4(),
        invocation_id=invocation_id,
    )
    executor = ToolExecutor(
        ToolRegistry((charge,)),
        side_effect_recorder=FlowToolSideEffectRecorder(ledger),
    )
    call = ToolCall(id="same-call", name="charge", arguments={})

    first = await executor.execute(call, context=context)
    replay = await executor.execute(call, context=context)

    assert first.status is ToolResultStatus.SUCCESS
    assert replay.status is ToolResultStatus.DENIED
    assert replay.error_code == "side_effect_replay_unsafe"
    assert execution_count == 1
