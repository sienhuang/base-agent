"""Single composition root for the application Agent."""

from pathlib import Path

from base_agent import Agent, AgentProfile, SkillRegistry

from agent_app.config import Settings
from agent_app.providers import build_provider
from agent_app.tools import TOOLS

SKILLS_ROOT = Path(__file__).parent / "skills"


def build_agent(settings: Settings | None = None) -> Agent:
    resolved = settings or Settings.from_env()
    registry = SkillRegistry.from_directory(SKILLS_ROOT)
    return Agent(
        profile=AgentProfile(
            id="starter-agent",
            instructions=(
                "Answer clearly. Use declared Tools when required and follow selected Skills."
            ),
            model=resolved.model,
            tools=("word_count",),
            skills=("text-analysis",),
            permissions=frozenset({"text:analyze"}),
            max_steps=8,
            max_tool_calls=8,
        ),
        model=build_provider(resolved),
        tools=TOOLS,
        skill_registry=registry,
    )
