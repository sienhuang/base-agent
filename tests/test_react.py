from __future__ import annotations

import json
from uuid import UUID

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    AgentRuntime,
    EventType,
    ModelResponse,
    ReActStrategy,
    ToolCall,
    tool,
)
from base_agent.testing import FakeModel


@tool
async def collect(source: str) -> dict[str, str]:
    """Collect one independent source."""

    return {"source": source, "value": f"{source}-value"}


@pytest.mark.asyncio
async def test_standalone_react_strategy_uses_shared_multi_tool_loop() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="collect-postgres",
                        name="collect",
                        arguments={"source": "postgres"},
                    ),
                    ToolCall(
                        id="collect-redis",
                        name="collect",
                        arguments={"source": "redis"},
                    ),
                )
            ),
            ModelResponse(
                content=json.dumps(
                    {
                        "success": True,
                        "result": "Collected PostgreSQL and Redis.",
                        "attachments": [],
                    }
                )
            ),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="standalone-react",
            instructions="Collect evidence.",
            tools=("collect",),
        ),
        model=model,
        tools=(collect,),
        runtime=AgentRuntime(strategy=ReActStrategy()),
    )

    result = await agent.run("collect both sources")
    events = await agent.events(UUID(result.metadata["run_id"]))
    event_types = [event.type for event in events]

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "Collected PostgreSQL and Redis."
    assert result.metadata["tool_calls"] == 2
    assert result.metadata["react"]["iteration"] == 2
    assert result.metadata["react"]["result"]["success"] is True
    assert len(model.requests) == 2
    assert len(model.requests[1].messages[-2:]) == 2
    assert event_types.count(EventType.REACT_ITERATION_STARTED) == 2
    assert event_types.count(EventType.REACT_ACTION_BATCH_SELECTED) == 1
    assert event_types.count(EventType.REACT_OBSERVATION_BATCH_RECORDED) == 1
    assert event_types.count(EventType.REACT_ITERATION_COMPLETED) == 2


@pytest.mark.asyncio
async def test_standalone_react_strategy_enforces_iteration_limit() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="collect-once",
                        name="collect",
                        arguments={"source": "postgres"},
                    ),
                )
            )
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="limited-react",
            instructions="Collect.",
            tools=("collect",),
        ),
        model=model,
        tools=(collect,),
        runtime=AgentRuntime(strategy=ReActStrategy(max_iterations=1)),
    )

    result = await agent.run("keep collecting")

    assert result.status is AgentResultStatus.FAILED
    assert result.error == "ReAct iteration limit reached (1)"
    assert len(model.requests) == 1


@pytest.mark.asyncio
async def test_standalone_react_strategy_rejects_unstructured_completion() -> None:
    agent = Agent(
        profile=AgentProfile(id="invalid-react", instructions="Work."),
        model=FakeModel([ModelResponse(content="plain text")]),
        runtime=AgentRuntime(strategy=ReActStrategy()),
    )

    result = await agent.run("work")

    assert result.status is AgentResultStatus.FAILED
    assert "invalid ReAct result" in (result.error or "")
