from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent
from mcp.types import Tool as SDKTool

from base_agent import ArtifactManager, InMemoryArtifactStore
from base_agent.governance import Confirmation, SideEffect
from base_agent.mcp import (
    FileMCPToolManifestStore,
    FileOAuthTokenStorage,
    InMemoryOAuthTokenStorage,
    MCPAuthConfig,
    MCPAuthorizationManager,
    MCPAuthType,
    MCPClient,
    MCPConnection,
    MCPServerConfig,
    MCPToolCallError,
    MCPToolPolicyConfig,
    MCPTransport,
    tools_from_manifest,
)
from base_agent.mcp.client import mcp_tool_name
from base_agent.tools import Tool, ToolContext


class FakeSession:
    def __init__(
        self,
        pages: dict[str | None, ListToolsResult],
        results: dict[str, CallToolResult] | None = None,
    ) -> None:
        self.pages = pages
        self.results = results or {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        return self.pages[cursor]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        self.calls.append((name, arguments))
        return self.results[name]


def remote_tool(name: str) -> SDKTool:
    return SDKTool(
        name=name,
        description=f"Execute {name}.",
        inputSchema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )


def server_config(**overrides: Any) -> MCPServerConfig:
    return MCPServerConfig(
        name="analytics",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=("server.py",),
        **overrides,
    )


@pytest.mark.asyncio
async def test_discovery_is_paginated_namespaced_and_policy_aware() -> None:
    session = FakeSession(
        {
            None: ListToolsResult(tools=[remote_tool("read-value")], nextCursor="2"),
            "2": ListToolsResult(tools=[remote_tool("write_value")]),
        }
    )
    config = server_config(
        tools={
            "write_value": MCPToolPolicyConfig(
                permissions=frozenset({"analytics:write"}),
                side_effect=SideEffect.MUTATING,
                confirmation=Confirmation.REQUIRED,
                timeout_seconds=12,
            )
        }
    )

    tools = await MCPClient(session, config).tools()

    assert [tool.spec.name for tool in tools] == [
        "mcp__analytics__read_value",
        "mcp__analytics__write_value",
    ]
    assert tools[0].side_effect is SideEffect.UNKNOWN
    assert tools[0].permissions == frozenset({"mcp:analytics:invoke"})
    assert tools[1].side_effect is SideEffect.MUTATING
    assert tools[1].confirmation is Confirmation.REQUIRED
    assert tools[1].permissions == frozenset({"analytics:write"})
    assert tools[1].timeout_seconds == 12
    assert isinstance(tools[0], Tool)


@pytest.mark.asyncio
async def test_arguments_are_validated_before_remote_call() -> None:
    session = FakeSession({None: ListToolsResult(tools=[remote_tool("calculate")])})
    tool = (await MCPClient(session, server_config()).tools())[0]

    with pytest.raises(ValueError, match="is not of type 'integer'"):
        await tool.invoke(
            {"value": "wrong"},
            ToolContext("run", "agent", "call"),
        )

    assert session.calls == []


@pytest.mark.asyncio
async def test_error_result_raises_tool_error() -> None:
    session = FakeSession(
        {None: ListToolsResult(tools=[remote_tool("calculate")])},
        {
            "calculate": CallToolResult(
                content=[TextContent(type="text", text="remote failed")],
                isError=True,
            )
        },
    )
    tool = (await MCPClient(session, server_config()).tools())[0]

    with pytest.raises(MCPToolCallError, match="remote failed"):
        await tool.invoke({"value": 1}, ToolContext("run", "agent", "call"))


@pytest.mark.asyncio
async def test_binary_content_becomes_artifact_receipt() -> None:
    session = FakeSession(
        {None: ListToolsResult(tools=[remote_tool("render")])},
        {
            "render": CallToolResult(
                content=[
                    ImageContent(
                        type="image",
                        data="aW1hZ2UtYnl0ZXM=",
                        mimeType="image/png",
                    )
                ],
                structuredContent={"answer": 42, "_meta": {"secret": True}},
            )
        },
    )
    tool = (await MCPClient(session, server_config()).tools())[0]
    store = InMemoryArtifactStore()
    artifacts = ArtifactManager(store, run_id="run", agent_id="agent")

    result = await tool.invoke(
        {"value": 1},
        ToolContext("run", "agent", "call", artifacts=artifacts),
    )

    receipt = result["content"][0]["artifact"]
    assert receipt["media_type"] == "image/png"
    assert receipt["size_bytes"] == len(b"image-bytes")
    assert await store.read(receipt["id"]) == b"image-bytes"
    assert result["structured_content"] == {"answer": 42}


def test_name_normalization_is_stable() -> None:
    assert mcp_tool_name("git-hub", "read.issue") == "mcp__git_hub__read_issue"
    assert len(mcp_tool_name("server", "x" * 100)) == 64
    assert mcp_tool_name("server", "读取") == mcp_tool_name("server", "读取")


def test_oauth_config_requires_redirect_uri() -> None:
    with pytest.raises(ValueError, match="redirect_uri"):
        MCPAuthConfig(type=MCPAuthType.OAUTH)


@pytest.mark.asyncio
async def test_file_oauth_storage_round_trip_and_permissions(tmp_path: Path) -> None:
    storage = FileOAuthTokenStorage(tmp_path, connection_name="github")
    tokens = OAuthToken(
        access_token="access",
        refresh_token="refresh",
        expires_in=3600,
    )
    client = OAuthClientInformationFull(
        redirect_uris=["http://127.0.0.1:8001/v1/mcp/oauth/callback"],
        client_id="client-id",
    )

    await storage.set_tokens(tokens)
    await storage.set_client_info(client)

    assert await storage.get_tokens() == tokens
    assert await storage.get_client_info() == client
    assert await storage.get_token_expiry_time() is not None
    assert os.stat(tmp_path / "github" / "tokens.json").st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "github" / "client.json").st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_real_stdio_connection_discovers_and_invokes_tool() -> None:
    fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
    config = MCPServerConfig(
        name="math",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=(str(fixture),),
    )

    async with MCPConnection(config) as connection:
        tools = await connection.client.tools()
        assert [tool.spec.name for tool in tools] == ["mcp__math__multiply"]
        result = await tools[0].invoke(
            {"left": 6, "right": 7},
            ToolContext("run", "agent", "call"),
        )

    assert result["structured_content"] == {"product": 42}


@pytest.mark.asyncio
async def test_cached_manifest_builds_on_demand_worker_tool(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "mcp_server.py"
    config = MCPServerConfig(
        name="math",
        transport=MCPTransport.STDIO,
        command=sys.executable,
        args=(str(fixture),),
    )
    manifests = FileMCPToolManifestStore(tmp_path)
    async with MCPConnection(config) as connection:
        discovered = await connection.client.tools()
        await manifests.save(config, discovered)

    tools = tools_from_manifest(config, manifests)
    assert [tool.spec.name for tool in tools] == ["mcp__math__multiply"]
    result = await tools[0].invoke(
        {"left": 8, "right": 9},
        ToolContext("run", "agent", "call"),
    )

    assert result["structured_content"] == {"product": 72}


@pytest.mark.asyncio
async def test_connection_close_must_use_owner_task() -> None:
    class Stack:
        async def aclose(self) -> None:
            return None

    connection = MCPConnection(server_config())
    connection._stack = Stack()  # type: ignore[assignment]
    connection._owner_task = asyncio.current_task()

    async def close_elsewhere() -> None:
        with pytest.raises(RuntimeError, match="same asyncio Task"):
            await connection.close()

    await asyncio.create_task(close_elsewhere())
    await connection.close()


@pytest.mark.asyncio
async def test_oauth_authorization_manager_bridges_redirect_and_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from base_agent.mcp import authorization as authorization_module

    config = MCPServerConfig(
        name="github",
        transport=MCPTransport.STREAMABLE_HTTP,
        url="https://mcp.example.test/mcp",
        auth=MCPAuthConfig(
            type=MCPAuthType.OAUTH,
            redirect_uri="http://127.0.0.1:8001/v1/mcp/oauth/callback",
        ),
    )
    authorized: list[str] = []

    class FakeClient:
        async def tools(self) -> tuple[Any, ...]:
            return ()

    class FakeConnection:
        def __init__(
            self,
            config: MCPServerConfig,
            **kwargs: Any,
        ) -> None:
            self.config = config
            self.client = FakeClient()
            self.redirect = kwargs["oauth_redirect_handler"]
            self.callback = kwargs["oauth_callback_handler"]

        async def __aenter__(self) -> FakeConnection:
            await self.redirect("https://auth.example/authorize?state=expected-state")
            code, state = await self.callback()
            assert (code, state) == ("authorization-code", "expected-state")
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr(authorization_module, "MCPConnection", FakeConnection)

    async def on_authorized(
        server: MCPServerConfig,
        tools: tuple[Any, ...],
    ) -> None:
        assert tools == ()
        authorized.append(server.name)

    storage = InMemoryOAuthTokenStorage()
    manager = MCPAuthorizationManager(
        {"github": config},
        storage_factory=lambda _: storage,
        on_authorized=on_authorized,
    )

    started = await manager.start("github")
    assert started.status == "authorization_required"
    assert started.authorization_url == (
        "https://auth.example/authorize?state=expected-state"
    )

    completed = await manager.complete(
        code="authorization-code",
        state="expected-state",
    )

    assert completed.connection_name == "github"
    assert completed.tools == ()
    assert authorized == ["github"]
