"""Smallest possible Agent: no network, tools, or external services."""

import asyncio

from base_agent import Agent, AgentDefinition, ModelResponse
from base_agent.testing import FakeModel


async def main() -> None:
    agent = Agent(
        definition=AgentDefinition(
            id="hello-agent",
            version="1.0.0",
            instructions="Answer clearly and briefly.",
        ),
        model=FakeModel([ModelResponse(content="Hello from base-agent!")]),
    )

    result = await agent.run("Say hello")
    print(result.output)


if __name__ == "__main__":
    asyncio.run(main())
