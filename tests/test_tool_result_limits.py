import json
from uuid import uuid4

import pytest

from base_agent import (
    Agent,
    AgentDefinition,
    AgentResultStatus,
    BoundedToolResultPolicy,
    EventType,
    FlowSideEffectPhase,
    FlowToolSideEffectRecorder,
    InMemoryFlowSideEffectLedger,
    ModelResponse,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolResultLimits,
    ToolResultStatus,
    ToolSideEffectMode,
    tool,
)
from base_agent.testing import FakeModel, ToolHarness


def test_bounded_policy_preserves_small_result_and_measures_utf8_bytes() -> None:
    policy = BoundedToolResultPolicy(ToolResultLimits(max_bytes=512))
    small = ToolResult(
        tool_name="lookup",
        status=ToolResultStatus.SUCCESS,
        data={"value": "ok"},
    )
    large = ToolResult(
        tool_name="lookup",
        status=ToolResultStatus.SUCCESS,
        data={"value": "界" * 200},
    )

    assert policy.enforce(small) == small
    replacement = policy.enforce(large)
    expected_size = len(large.model_dump_json().encode())

    assert replacement.status is ToolResultStatus.ERROR
    assert replacement.error_code == "tool_result_too_large"
    assert replacement.data == {
        "original_size_bytes": expected_size,
        "limit_bytes": 512,
        "original_status": "success",
        "overflow_action": "rejected",
    }
    assert "界" not in replacement.model_dump_json()


@pytest.mark.asyncio
async def test_tool_harness_applies_the_same_result_limit() -> None:
    secret = "private-payload-" * 100

    @tool
    async def oversized() -> str:
        return secret

    result = await ToolHarness(
        (oversized,),
        max_result_bytes=512,
    ).run("oversized")

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "tool_result_too_large"
    assert secret not in result.model_dump_json()


@pytest.mark.asyncio
async def test_runtime_never_puts_oversized_tool_payload_in_context_or_events() -> None:
    secret = "TOP_SECRET_RESULT_" * 100

    @tool(side_effect=ToolSideEffectMode.IDEMPOTENT)
    async def export(context: ToolContext) -> str:
        assert context.idempotency_key is not None
        return secret

    definition = AgentDefinition(
        id="bounded-export-agent",
        version="1.0.0",
        instructions="Export safely.",
        tools=("export",),
        max_tool_result_bytes=512,
    )
    ledger = InMemoryFlowSideEffectLedger()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="export-call",
                        name="export",
                        arguments={},
                    ),
                )
            ),
            ModelResponse(content="result was too large"),
        ]
    )
    agent = Agent(
        definition=definition,
        model=model,
        tools=(export,),
        side_effect_recorder=FlowToolSideEffectRecorder(ledger),
    )
    invocation_id = uuid4()

    result = await agent.execute_invocation(
        "export",
        flow_run_id=uuid4(),
        invocation_id=invocation_id,
        agent_key="exporter",
    )

    assert result.status is AgentResultStatus.COMPLETED
    tool_message = model.requests[1].messages[-1]
    payload = json.loads(tool_message.content or "{}")
    assert payload["error_code"] == "tool_result_too_large"
    assert secret not in (tool_message.content or "")
    failed_event = next(
        event
        for event in await agent.events(invocation_id)
        if event.type is EventType.TOOL_FAILED
    )
    assert failed_event.data["result"]["error_code"] == (
        "tool_result_too_large"
    )
    assert secret not in failed_event.model_dump_json()
    effects = await ledger.list_for_invocation(invocation_id)
    assert effects[0].phase is FlowSideEffectPhase.CONFIRMED
