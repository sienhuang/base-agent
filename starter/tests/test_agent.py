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
    assert agent.definition is not None
    assert agent.definition.version == "1.0.0"


@pytest.mark.asyncio
async def test_offline_provider_is_reusable_across_runs() -> None:
    agent = build_agent(Settings(provider="offline"))

    first = await agent.run("one", skills=("text-analysis",))
    second = await agent.run("one two", skills=("text-analysis",))

    assert first.status is AgentResultStatus.COMPLETED
    assert second.status is AgentResultStatus.COMPLETED
    assert first.output != second.output


@pytest.mark.asyncio
async def test_offline_agent_supports_run_backed_conversation_turns() -> None:
    agent = build_agent(Settings(provider="offline"))
    conversation = await agent.create_conversation()

    first = await agent.run("my name is Xiao Ming", conversation_id=conversation.id)
    second = await agent.run("what is my name", conversation_id=conversation.id)
    turns = await agent.conversation_turns(conversation.id)

    assert [turn.sequence for turn in turns] == [1, 2]
    assert turns[0].run_id != turns[1].run_id
    assert second.messages[1].content == "my name is Xiao Ming"
    assert second.messages[2].content == first.output


@pytest.mark.asyncio
async def test_offline_agent_can_generate_and_execute_plan() -> None:
    agent = build_agent(Settings(provider="offline"))

    result = await agent.run("complete planned work", planning=True)

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "Offline planned Run completed."
    assert result.metadata["planning_requested"] is True
    assert result.metadata["plan"]["status"] == "completed"
    assert result.metadata["model_calls"] == 4
    assert "replan_count" not in result.metadata["plan"]["metadata"]


@pytest.mark.asyncio
async def test_offline_agent_can_run_as_standalone_react_agent() -> None:
    agent = build_agent(Settings(provider="offline"), react=True)

    result = await agent.run("count this text", skills=("text-analysis",))
    events = await agent.events(UUID(result.metadata["run_id"]))

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == (
        "Offline starter completed the Tool loop: 3 words, 15 characters."
    )
    assert result.metadata["react"]["iteration"] == 2
    assert EventType.REACT_ACTION_BATCH_SELECTED in [
        event.type for event in events
    ]


def test_starter_composes_explicit_data_and_coding_bundles() -> None:
    agent = build_agent(
        Settings(
            provider="offline",
            enable_coding=True,
            sandbox_image="python:3.12",
            enable_web_search=True,
            brave_search_api_key="test-search-key",
            enable_mtbi=True,
        )
    )

    assert {
        "sandbox_read_text",
        "sandbox_write_text",
        "sandbox_execute",
        "web_search",
        "data_list_tables",
        "data_describe_table",
        "data_query",
    }.issubset(agent.profile.tools)
    assert {
        "sandbox:read",
        "sandbox:write",
        "sandbox:execute",
        "web:search",
        "data:read",
    }.issubset(agent.profile.permissions)
    assert [resource.name for resource in agent.resources] == ["coding-sandbox"]


def test_starter_requires_configuration_for_enabled_external_capabilities() -> None:
    with pytest.raises(ValueError, match="AGENT_SANDBOX_IMAGE"):
        build_agent(Settings(provider="offline", enable_coding=True))

    with pytest.raises(ValueError, match="BRAVE_SEARCH_API_KEY"):
        build_agent(Settings(provider="offline", enable_web_search=True))
