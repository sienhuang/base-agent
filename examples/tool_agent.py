"""A complete Model -> Tool -> Model loop using deterministic responses."""

import asyncio

from base_agent import Agent, AgentProfile, ModelResponse, ToolCall, tool
from base_agent.testing import FakeModel


@tool
async def get_weather(city: str) -> dict[str, str]:
    """Get the current weather for a city."""
    return {"city": city, "condition": "sunny"}


async def main() -> None:
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
            ModelResponse(content="Shanghai is sunny."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="weather-agent",
            instructions="Use the weather tool before answering.",
            tools=("get_weather",),
        ),
        model=model,
        tools=[get_weather],
    )

    result = await agent.run("What is the weather in Shanghai?")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
