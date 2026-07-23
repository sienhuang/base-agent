from uuid import UUID

import pytest
from base_agent import AgentResultStatus, EventType

from agent_app import Settings, build_agent


@pytest.mark.asyncio
async def test_offline_agent_runs_model_tool_model_with_selected_skill() -> None:
    agent = build_agent(Settings(provider="offline"))

    result = await agent.run("hello reusable agent", skills=("text-analysis",))
    run_id = UUID(result.metadata["run_id"])
    run = await agent.get_run(run_id)
    events = await agent.events(run_id)

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == (
        "Offline starter completed the Tool loop: 3 words, 20 characters."
    )
    assert run.skills[0].name == "text-analysis"
    assert EventType.TOOL_COMPLETED in [event.type for event in events]


@pytest.mark.asyncio
async def test_offline_provider_is_reusable_across_runs() -> None:
    agent = build_agent(Settings(provider="offline"))

    first = await agent.run("one", skills=("text-analysis",))
    second = await agent.run("one two", skills=("text-analysis",))

    assert first.status is AgentResultStatus.COMPLETED
    assert second.status is AgentResultStatus.COMPLETED
    assert first.output != second.output
