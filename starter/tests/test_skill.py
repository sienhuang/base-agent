import pytest
from base_agent import ToolResultStatus
from base_agent.testing import SkillHarness, ToolHarness

from agent_app import Settings, build_agent
from agent_app.tools import TOOLS


@pytest.mark.asyncio
async def test_example_tool_and_skill_use_production_validation_paths() -> None:
    agent = build_agent(Settings(provider="offline"))
    tool_result = await ToolHarness(TOOLS).run(
        "word_count",
        {"text": "one two"},
        permissions=frozenset({"text:analyze"}),
    )
    skill_report = SkillHarness(
        agent.skill_registry,
        TOOLS,
    ).validate("text-analysis", profile=agent.profile)

    assert tool_result.status is ToolResultStatus.SUCCESS
    assert tool_result.data == {"words": 2, "characters": 7}
    assert skill_report.valid, skill_report.issues
