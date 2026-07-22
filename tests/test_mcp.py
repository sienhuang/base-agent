import json
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.types import CallToolResult, ListToolsResult, TextContent
from mcp.types import Tool as SDKTool

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    ModelResponse,
    ToolCall,
    ToolExecutor,
    ToolRegistry,
    ToolResultStatus,
)
from base_agent.mcp import MCPClient, MCPTool, StdioServerParameters, stdio_mcp_client
from base_agent.testing import FakeModel


class FakeSession:
    def __init__(
        self,
        pages: dict[str | None, ListToolsResult],
        results: dict[str, CallToolResult] | None = None,
    ) -> None:
        self.pages = pages
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def list_tools(self, cursor: str | None, /) -> ListToolsResult:
        return self.pages[cursor]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        return self.results[name]


def remote_tool(name: str, *, required: bool = True) -> SDKTool:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"value": {"type": "integer"}},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = ["value"]
    return SDKTool(name=name, description=f"Execute {name}.", inputSchema=schema)


@pytest.mark.asyncio
async def test_discovery_is_paginated_namespaced_and_permissioned() -> None:
    session = FakeSession(
        {
            None: ListToolsResult(tools=[remote_tool("first")], nextCursor="page-2"),
            "page-2": ListToolsResult(tools=[remote_tool("second")]),
        }
    )
    client = MCPClient(session, server_name="analytics")

    tools = await client.tools(
        name_prefix="analytics",
        permissions=frozenset({"mcp:analytics"}),
        permissions_by_tool={"second": frozenset({"mcp:analytics:write"})},
        timeout_seconds=12,
    )

    assert [tool.definition.name for tool in tools] == [
        "analytics.first",
        "analytics.second",
    ]
    assert tools[0].remote_name == "first"
    assert tools[0].permissions == frozenset({"mcp:analytics"})
    assert tools[1].permissions == frozenset({"mcp:analytics:write"})
    assert tools[1].timeout_seconds == 12


@pytest.mark.asyncio
async def test_mcp_arguments_are_validated_before_remote_execution() -> None:
    session = FakeSession({None: ListToolsResult(tools=[])})
    tool = MCPTool(remote_tool("calculate"), session)
    executor = ToolExecutor(ToolRegistry([tool]))

    denied = await executor.execute(
        ToolCall(id="call-1", name="calculate", arguments={"value": 2})
    )
    invalid = await executor.execute(
        ToolCall(id="call-2", name="calculate", arguments={"value": "two"}),
        granted_permissions=frozenset({"mcp:invoke"}),
    )

    assert denied.status is ToolResultStatus.DENIED
    assert invalid.status is ToolResultStatus.INVALID_ARGUMENTS
    assert invalid.error_code == "invalid_arguments"
    assert session.calls == []


@pytest.mark.asyncio
async def test_mcp_results_are_normalized_without_private_metadata() -> None:
    result = CallToolResult(
        content=[
            TextContent(
                type="text",
                text="complete",
                _meta={"server_secret": "must-not-reach-model"},
            )
        ],
        structuredContent={"answer": 42},
        _meta={"trace_id": "private"},
    )
    session = FakeSession(
        {None: ListToolsResult(tools=[])},
        {"calculate": result},
    )
    tool = MCPTool(remote_tool("calculate"), session)

    payload = await tool.invoke({"value": 21})

    assert payload == {
        "content": [{"type": "text", "text": "complete"}],
        "structured_content": {"answer": 42},
    }
    assert "private" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_mcp_tool_error_becomes_structured_executor_error() -> None:
    session = FakeSession(
        {None: ListToolsResult(tools=[])},
        {
            "calculate": CallToolResult(
                content=[TextContent(type="text", text="remote calculation failed")],
                isError=True,
            )
        },
    )
    tool = MCPTool(remote_tool("calculate"), session)

    result = await ToolExecutor(ToolRegistry([tool])).execute(
        ToolCall(id="call-1", name="calculate", arguments={"value": 2}),
        granted_permissions=frozenset({"mcp:invoke"}),
    )

    assert result.status is ToolResultStatus.ERROR
    assert result.error_code == "tool_execution_error"
    assert result.message == "remote calculation failed"


@pytest.mark.asyncio
async def test_real_stdio_mcp_tool_runs_through_agent() -> None:
    server = Path(__file__).parent / "fixtures" / "mcp_server.py"
    parameters = StdioServerParameters(command=sys.executable, args=[str(server)])

    async with stdio_mcp_client(parameters) as client:
        tools = await client.tools(name_prefix="math")
        model = FakeModel(
            [
                ModelResponse(
                    tool_calls=(
                        ToolCall(
                            id="multiply-call",
                            name="math.multiply",
                            arguments={"left": 6, "right": 7},
                        ),
                    )
                ),
                ModelResponse(content="The product is 42."),
            ]
        )
        agent = Agent(
            profile=AgentProfile(
                id="mcp-agent",
                instructions="Use the remote calculator.",
                tools=("math.multiply",),
                permissions=frozenset({"mcp:invoke"}),
            ),
            model=model,
            tools=tools,
        )

        result = await agent.run("What is 6 times 7?")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "The product is 42."
    assert model.requests[0].tools[0].name == "math.multiply"
    tool_result = json.loads(model.requests[1].messages[-1].content or "{}")
    assert tool_result["status"] == "success"
    assert tool_result["data"]["structured_content"] == {"product": 42}
