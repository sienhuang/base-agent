"""Load a versioned SKILL.md and execute it with a restricted Tool set."""

import asyncio
from pathlib import Path

from base_agent import Agent, AgentProfile, ModelResponse, SkillRegistry, ToolCall, tool
from base_agent.testing import FakeModel


@tool(permissions=frozenset({"weather:read"}))
async def get_weather(city: str) -> dict[str, str]:
    """Get the current weather for a city."""
    return {"city": city, "condition": "sunny", "source": "offline-example"}


async def main() -> None:
    skill_root = Path(__file__).parent / "skills"
    registry = SkillRegistry.from_directory(skill_root)
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="weather-1",
                        name="get_weather",
                        arguments={"city": "Shanghai"},
                    ),
                )
            ),
            ModelResponse(content="Verified result: Shanghai is sunny."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="weather-skill-agent",
            instructions="Follow selected Skills exactly.",
            tools=("get_weather",),
            skills=("weather-analysis",),
            permissions=frozenset({"weather:read"}),
        ),
        model=model,
        tools=[get_weather],
        skill_registry=registry,
    )

    result = await agent.run(
        "Analyze the weather in Shanghai.",
        skills=("weather-analysis",),
    )
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
