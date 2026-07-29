"""Single composition root for the application Agent."""

from pathlib import Path

from base_agent import Agent, AgentProfile, AgentRuntime, ReActStrategy, SkillRegistry

from agent_app.config import Settings
from agent_app.providers import build_provider
from agent_app.tools import ENABLED_PERMISSIONS, ENABLED_TOOL_NAMES, REGISTERED_TOOLS

SKILLS_ROOT = Path(__file__).parent / "skills"


def build_agent(
    settings: Settings | None = None,
    *,
    react: bool = False,
) -> Agent:
    resolved = settings or Settings.from_env()
    registry = SkillRegistry.from_directory(SKILLS_ROOT)
    return Agent(
        profile=AgentProfile(
            id="starter-agent",
            instructions=(
                "Answer clearly. Use declared Tools when required and follow selected Skills."
            ),
            model=resolved.model,
            tools=ENABLED_TOOL_NAMES,
            skills=("text-analysis",),
            permissions=ENABLED_PERMISSIONS,
            max_steps=8,
            max_tool_calls=8,
        ),
        model=build_provider(resolved),
        tools=REGISTERED_TOOLS,
        skill_registry=registry,
        runtime=AgentRuntime(strategy=ReActStrategy()) if react else None,
    )
