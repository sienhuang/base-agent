"""Suspend a Run for human input and resume the same model/tool conversation."""

import asyncio
from uuid import UUID

from base_agent import (
    Agent,
    AgentProfile,
    ModelResponse,
    ToolCall,
    WaitForInput,
    tool,
)
from base_agent.testing import FakeModel


@tool
async def ask_user(question: str) -> WaitForInput:
    """Ask the user for information required to continue."""
    return WaitForInput(prompt=question)


async def main() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="region-question",
                        name="ask_user",
                        arguments={"question": "Which region should I use?"},
                    ),
                )
            ),
            ModelResponse(content="The report will use APAC."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="interactive-agent",
            instructions="Ask for missing information.",
            tools=("ask_user",),
        ),
        model=model,
        tools=(ask_user,),
    )

    waiting = await agent.run("Build the report")
    print(waiting.metadata["pending_input"]["prompt"])

    run_id = UUID(str(waiting.metadata["run_id"]))
    completed = await agent.resume(run_id, "APAC")
    print(completed.output)


if __name__ == "__main__":
    asyncio.run(main())
