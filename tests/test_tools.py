import asyncio
import json

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    MessageRole,
    ModelResponse,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    ToolResultStatus,
    tool,
)
from base_agent.testing import FakeModel
from base_agent.tools import DuplicateToolError, ToolNotFoundError


def test_tool_decorator_generates_schema_from_function_signature() -> None:
    @tool
    async def weather(city: str, units: str = "metric") -> dict[str, str]:
        """Get weather for a city."""
        return {"city": city, "units": units}

    schema = weather.definition.input_schema

    assert weather.definition.name == "weather"
    assert weather.definition.description == "Get weather for a city."
    assert schema["type"] == "object"
    assert schema["required"] == ["city"]
    assert schema["properties"]["units"]["default"] == "metric"
    assert schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_executor_supports_sync_tools_without_blocking_contract_changes() -> None:
    @tool
    def add(left: int, right: int) -> int:
        return left + right

    result = await ToolExecutor(ToolRegistry([add])).execute(
        ToolCall(id="call-1", name="add", arguments={"left": 2, "right": 3})
    )

    assert result.status is ToolResultStatus.SUCCESS
    assert result.data == 5


@pytest.mark.asyncio
async def test_invalid_arguments_are_rejected_before_function_execution() -> None:
    executed = False

    @tool
    async def double(value: int) -> int:
        nonlocal executed
        executed = True
        return value * 2

    result = await ToolExecutor(ToolRegistry([double])).execute(
        ToolCall(id="call-1", name="double", arguments={"value": "not-an-integer"})
    )

    assert result.status is ToolResultStatus.INVALID_ARGUMENTS
    assert result.error_code == "invalid_arguments"
    assert executed is False


@pytest.mark.asyncio
async def test_permissions_are_checked_before_execution() -> None:
    @tool(permissions=frozenset({"weather:read"}))
    async def weather(city: str) -> str:
        return city

    executor = ToolExecutor(ToolRegistry([weather]))
    denied = await executor.execute(
        ToolCall(id="call-1", name="weather", arguments={"city": "上海"})
    )
    allowed = await executor.execute(
        ToolCall(id="call-2", name="weather", arguments={"city": "上海"}),
        granted_permissions=frozenset({"weather:read"}),
    )

    assert denied.status is ToolResultStatus.DENIED
    assert denied.message == "missing tool permissions: weather:read"
    assert allowed.status is ToolResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_tool_timeout_and_exception_are_structured_results() -> None:
    @tool(timeout_seconds=0.01)
    async def slow() -> str:
        await asyncio.sleep(0.05)
        return "late"

    @tool
    async def broken() -> str:
        raise RuntimeError("broken dependency")

    executor = ToolExecutor(ToolRegistry([slow, broken]))

    timed_out = await executor.execute(ToolCall(id="call-1", name="slow", arguments={}))
    failed = await executor.execute(ToolCall(id="call-2", name="broken", arguments={}))
    missing = await executor.execute(ToolCall(id="call-3", name="missing", arguments={}))

    assert timed_out.status is ToolResultStatus.TIMEOUT
    assert failed.status is ToolResultStatus.ERROR
    assert failed.message == "broken dependency"
    assert missing.status is ToolResultStatus.NOT_FOUND


def test_registry_rejects_duplicates_and_missing_profile_tools() -> None:
    @tool
    async def demo() -> str:
        return "ok"

    with pytest.raises(DuplicateToolError, match="already registered"):
        ToolRegistry([demo, demo])

    with pytest.raises(ToolNotFoundError, match="not registered"):
        Agent(
            profile=AgentProfile(
                id="assistant",
                instructions="Be helpful.",
                tools=("missing",),
            ),
            model=FakeModel([ModelResponse(content="unused")]),
        )


@pytest.mark.asyncio
async def test_agent_runs_full_model_tool_model_loop() -> None:
    @tool(permissions=frozenset({"weather:read"}))
    async def weather(city: str) -> dict[str, str]:
        """Get weather for a city."""
        return {"city": city, "condition": "sunny"}

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-1", name="weather", arguments={"city": "上海"}),
                )
            ),
            ModelResponse(content="上海天气晴朗。"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("weather",),
            permissions=frozenset({"weather:read"}),
        ),
        model=model,
        tools=[weather],
    )

    result = await agent.run("上海天气怎么样？")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "上海天气晴朗。"
    assert result.metadata["steps"] == 2
    assert len(model.requests) == 2
    assert model.requests[0].tools[0].name == "weather"
    assert [message.role for message in model.requests[1].messages[-2:]] == [
        MessageRole.ASSISTANT,
        MessageRole.TOOL,
    ]
    tool_payload = json.loads(model.requests[1].messages[-1].content or "{}")
    assert tool_payload["status"] == "success"
    assert tool_payload["data"]["condition"] == "sunny"


@pytest.mark.asyncio
async def test_all_tool_calls_are_executed_in_model_order() -> None:
    execution_order: list[str] = []

    @tool
    async def record(value: str) -> str:
        execution_order.append(value)
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(id="call-1", name="record", arguments={"value": "first"}),
                    ToolCall(id="call-2", name="record", arguments={"value": "second"}),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Be helpful.",
            tools=("record",),
        ),
        model=model,
        tools=[record],
    )

    result = await agent.run("record values")

    assert result.status is AgentResultStatus.COMPLETED
    assert execution_order == ["first", "second"]
    assert [message.tool_call_id for message in model.requests[1].messages[-2:]] == [
        "call-1",
        "call-2",
    ]
