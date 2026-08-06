from uuid import uuid4

import pytest
from pydantic import ValidationError

from base_agent import (
    AgentDefinition,
    AgentHandoff,
    AgentInvocationInput,
    AgentInvocationRequest,
    AgentInvocationResume,
    AgentInvoker,
    AgentResultStatus,
    FlowAgent,
    FlowDefinition,
    PendingInput,
)
from base_agent.testing import (
    ScriptedAgentInvoker,
    ScriptedAgentInvokerExhaustedError,
    ScriptedAgentOutcome,
)


def test_flow_definition_resolves_named_agents_and_has_stable_fingerprint() -> None:
    researcher = AgentDefinition(
        id="research-agent",
        version="1.0.0",
        instructions="Research the topic.",
    )
    writer = AgentDefinition(
        id="writer-agent",
        version="1.0.0",
        instructions="Write the report.",
    )
    definition = FlowDefinition(
        id="research-report",
        version="1.0.0",
        agents=(
            FlowAgent(key="researcher", definition=researcher),
            FlowAgent(key="writer", definition=writer),
        ),
        strategy="sequential",
    )
    equivalent = FlowDefinition.model_validate_json(definition.model_dump_json())

    assert definition.agent("researcher") is researcher
    assert equivalent.fingerprint == definition.fingerprint
    assert len(definition.fingerprint) == 64
    with pytest.raises(KeyError, match="no Agent named 'reviewer'"):
        definition.agent("reviewer")


def test_flow_definition_rejects_duplicate_agent_keys() -> None:
    definition = AgentDefinition(
        id="shared-agent",
        version="1.0.0",
        instructions="Work.",
    )

    with pytest.raises(ValidationError, match="agent keys must be unique"):
        FlowDefinition(
            id="invalid-flow",
            version="1.0.0",
            agents=(
                FlowAgent(key="worker", definition=definition),
                FlowAgent(key="worker", definition=definition),
            ),
            strategy="sequential",
        )


def test_handoff_is_explicit_json_context_without_message_history() -> None:
    handoff = AgentHandoff(
        source_agent_key="researcher",
        summary="Three sources support the conclusion.",
        data={
            "confidence": 0.9,
            "source_ids": ["source-1", "source-2", "source-3"],
        },
    )
    invocation_input = AgentInvocationInput(
        prompt="Write the final report.",
        handoff=handoff,
    )

    restored = AgentInvocationInput.model_validate_json(
        invocation_input.model_dump_json()
    )

    assert restored == invocation_input
    assert not hasattr(restored, "messages")


@pytest.mark.asyncio
async def test_scripted_agent_invoker_records_requests_and_wait_resume() -> None:
    flow_run_id = uuid4()
    pending = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Which region?",
    )
    invoker = ScriptedAgentInvoker(
        {
            "researcher": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.WAITING,
                    pending_input=pending,
                ),
                ScriptedAgentOutcome(
                    status=AgentResultStatus.COMPLETED,
                    output="APAC research complete.",
                ),
            ),
        }
    )
    assert isinstance(invoker, AgentInvoker)
    request = AgentInvocationRequest(
        flow_run_id=flow_run_id,
        sequence=1,
        agent_key="researcher",
        definition_id="research-agent",
        definition_version="1.0.0",
        definition_fingerprint="0" * 64,
        input=AgentInvocationInput(prompt="Research the market."),
    )

    waiting = await invoker.invoke(request)
    completed = await invoker.resume(
        AgentInvocationResume(
            flow_run_id=flow_run_id,
            invocation_id=request.invocation_id,
            agent_key=request.agent_key,
            user_input="APAC",
        )
    )

    assert waiting.status is AgentResultStatus.WAITING
    assert completed.status is AgentResultStatus.COMPLETED
    assert completed.output == "APAC research complete."
    assert completed.invocation_id == waiting.invocation_id
    assert invoker.requests == (request,)
    assert invoker.resumes[0].user_input == "APAC"


@pytest.mark.asyncio
async def test_scripted_agent_invoker_fails_loudly_when_exhausted() -> None:
    invoker = ScriptedAgentInvoker({"worker": ()})
    request = AgentInvocationRequest(
        flow_run_id=uuid4(),
        sequence=1,
        agent_key="worker",
        definition_id="worker-agent",
        definition_version="1.0.0",
        definition_fingerprint="0" * 64,
        input=AgentInvocationInput(prompt="Work."),
    )

    with pytest.raises(ScriptedAgentInvokerExhaustedError, match="no scripted outcome"):
        await invoker.invoke(request)


@pytest.mark.asyncio
async def test_failed_scripted_resume_does_not_consume_waiting_invocation() -> None:
    pending = PendingInput(
        tool_call_id="call-1",
        tool_name="ask_user",
        prompt="Continue?",
    )
    invoker = ScriptedAgentInvoker(
        {
            "worker": (
                ScriptedAgentOutcome(
                    status=AgentResultStatus.WAITING,
                    pending_input=pending,
                ),
            ),
        }
    )
    request = AgentInvocationRequest(
        flow_run_id=uuid4(),
        sequence=1,
        agent_key="worker",
        definition_id="worker-agent",
        definition_version="1.0.0",
        definition_fingerprint="0" * 64,
        input=AgentInvocationInput(prompt="Work."),
    )
    await invoker.invoke(request)
    resume = AgentInvocationResume(
        flow_run_id=request.flow_run_id,
        invocation_id=request.invocation_id,
        agent_key=request.agent_key,
        user_input="yes",
    )

    with pytest.raises(ScriptedAgentInvokerExhaustedError):
        await invoker.resume(resume)
    with pytest.raises(ScriptedAgentInvokerExhaustedError):
        await invoker.resume(resume)


def test_waiting_invocation_result_requires_pending_input() -> None:
    from base_agent import AgentInvocationResult

    with pytest.raises(ValidationError, match="requires pending_input"):
        AgentInvocationResult(
            flow_run_id=uuid4(),
            invocation_id=uuid4(),
            agent_key="worker",
            status=AgentResultStatus.WAITING,
        )
