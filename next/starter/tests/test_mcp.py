from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from base_agent.mcp import (
    FileMCPToolManifestStore,
    MCPClient,
    load_mcp_server_configs,
)
from mcp.types import CallToolResult, ListToolsResult
from mcp.types import Tool as SDKTool

from agent_app.asgi import create_app
from agent_app.config import Settings
from agent_app.mcp_sync import MCPSyncError, run_sync, sync_mcp_tools

MCP_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "mcp_server.py"


class ManifestSession:
    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        del cursor
        return ListToolsResult(tools=[])

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        raise AssertionError((name, arguments))


async def test_starter_loads_cached_mcp_tools_and_protects_control_routes(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "\n".join(
            (
                "mcp_servers:",
                "  math:",
                "    transport: stdio",
                f"    command: {sys.executable}",
                "    args: [server.py]",
            )
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"
    config = load_mcp_server_configs(config_path)[0]
    remote = SDKTool(
        name="multiply",
        description="Multiply two values.",
        inputSchema={
            "type": "object",
            "properties": {
                "left": {"type": "integer"},
                "right": {"type": "integer"},
            },
            "required": ["left", "right"],
        },
    )
    tools = MCPClient(ManifestSession(), config).adapt_tools((remote,))
    manifests = FileMCPToolManifestStore(state_root / "manifests")
    await manifests.save(config, tools)
    settings = Settings(
        mcp_config_path=config_path,
        mcp_state_root=state_root,
        operator_token="operator-secret",
    )

    app = create_app(settings)
    agent = app.state.run_registry.executor.agent

    assert "mcp__math__multiply" in [
        item.name for item in agent.snapshot.tool_specs
    ]
    assert "mcp:math:invoke" in agent.definition.permissions

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        denied = await client.get("/v1/mcp/connections/math")
        allowed = await client.get(
            "/v1/mcp/connections/math",
            headers={"Authorization": "Bearer operator-secret"},
        )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "authorized"


async def test_mcp_sync_is_a_successful_noop_without_configuration(capsys) -> None:
    settings = Settings(mcp_config_path=None)

    report = await sync_mcp_tools(settings)
    exit_code = await run_sync(settings)

    assert report.configured is False
    assert report.servers == ()
    assert exit_code == 0
    assert capsys.readouterr().out == "No MCP configuration; nothing to sync.\n"


async def test_mcp_sync_discovers_multiple_servers_before_saving_manifests(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "\n".join(
            (
                "mcp_servers:",
                "  first_math:",
                "    transport: stdio",
                f"    command: {sys.executable}",
                f"    args: [{MCP_FIXTURE}]",
                "  second_math:",
                "    transport: stdio",
                f"    command: {sys.executable}",
                f"    args: [{MCP_FIXTURE}]",
            )
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"

    report = await sync_mcp_tools(
        Settings(mcp_config_path=config_path, mcp_state_root=state_root)
    )

    assert report.configured is True
    assert [server.connection_name for server in report.servers] == [
        "first_math",
        "second_math",
    ]
    assert [server.tool_names for server in report.servers] == [
        ("mcp__first_math__multiply",),
        ("mcp__second_math__multiply",),
    ]
    manifests = FileMCPToolManifestStore(state_root / "manifests")
    for config in load_mcp_server_configs(config_path):
        assert [tool.name for tool in manifests.load(config)] == ["multiply"]


async def test_mcp_sync_does_not_save_partial_discovery(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "\n".join(
            (
                "mcp_servers:",
                "  available:",
                "    transport: stdio",
                f"    command: {sys.executable}",
                f"    args: [{MCP_FIXTURE}]",
                "  unavailable:",
                "    transport: stdio",
                "    command: definitely-missing-mcp-server-executable",
            )
        ),
        encoding="utf-8",
    )
    state_root = tmp_path / "state"

    with pytest.raises(MCPSyncError, match="unavailable"):
        await sync_mcp_tools(
            Settings(mcp_config_path=config_path, mcp_state_root=state_root)
        )

    assert not (state_root / "manifests" / "available.json").exists()


async def test_mcp_sync_requires_existing_oauth_authorization(tmp_path: Path) -> None:
    config_path = tmp_path / "mcp.yaml"
    config_path.write_text(
        "\n".join(
            (
                "mcp_servers:",
                "  remote:",
                "    transport: streamable_http",
                "    url: https://mcp.example.test/mcp",
                "    auth:",
                "      type: oauth",
                "      redirect_uri: http://127.0.0.1:8001/v1/mcp/oauth/callback",
            )
        ),
        encoding="utf-8",
    )

    with pytest.raises(MCPSyncError, match="interactive OAuth authorization"):
        await sync_mcp_tools(
            Settings(mcp_config_path=config_path, mcp_state_root=tmp_path / "state")
        )
