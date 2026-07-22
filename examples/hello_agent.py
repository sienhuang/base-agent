"""Smallest possible Agent: no network, tools, or external services."""

import asyncio

from base_agent import Agent, AgentProfile, ModelResponse
from base_agent.testing import FakeModel


async def main() -> None:
    agent = Agent(
        profile=AgentProfile(
            id="hello-agent",
            instructions="Answer clearly and briefly.",
        ),
        model=FakeModel([ModelResponse(content="Hello from base-agent!")]),
    )

    result = await agent.run("Say hello")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
