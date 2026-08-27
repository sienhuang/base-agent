"""Synchronize configured MCP Tool manifests before API and Worker startup."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent_app.config import Settings

if TYPE_CHECKING:
    from base_agent.mcp import MCPServerConfig, MCPTool

    from agent_app.mcp import MCPRuntime


class MCPSyncError(RuntimeError):
    """One or more configured MCP servers could not be synchronized."""


@dataclass(frozen=True, slots=True)
class MCPServerSyncResult:
    connection_name: str
    tool_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MCPSyncReport:
    configured: bool
    servers: tuple[MCPServerSyncResult, ...] = ()

    @property
    def tool_count(self) -> int:
        return sum(len(server.tool_names) for server in self.servers)


async def sync_mcp_tools(settings: Settings) -> MCPSyncReport:
    """Discover every enabled MCP server, then persist their Tool manifests."""

    if settings.mcp_config_path is None:
        return MCPSyncReport(configured=False)

    from agent_app.mcp import build_mcp_runtime

    runtime = build_mcp_runtime(settings)
    if runtime is None:
        return MCPSyncReport(configured=False)
    try:
        if not runtime.configs:
            return MCPSyncReport(configured=True)
        discovered = await asyncio.gather(
            *(_discover_tools(runtime, config) for config in runtime.configs),
            return_exceptions=True,
        )
        failures = [
            (config.name, result)
            for config, result in zip(runtime.configs, discovered, strict=True)
            if isinstance(result, Exception)
        ]
        if failures:
            detail = "; ".join(
                f"{name}: {type(error).__name__}: {error}"
                for name, error in failures
            )
            raise MCPSyncError(f"MCP synchronization failed: {detail}")

        synchronized: list[MCPServerSyncResult] = []
        for config, tools in zip(runtime.configs, discovered, strict=True):
            assert not isinstance(tools, BaseException)
            await runtime.manifests.save(config, tools)
            synchronized.append(
                MCPServerSyncResult(
                    connection_name=config.name,
                    tool_names=tuple(tool.spec.name for tool in tools),
                )
            )
        return MCPSyncReport(configured=True, servers=tuple(synchronized))
    finally:
        await runtime.close()


async def _discover_tools(
    runtime: MCPRuntime,
    config: MCPServerConfig,
) -> tuple[MCPTool, ...]:
    from base_agent.mcp import MCPAuthType, MCPConnection

    storage = runtime.storage_for(config)
    if (
        config.auth.type is MCPAuthType.OAUTH
        and not await runtime.authorization.token_available(config.name)
    ):
        raise MCPSyncError(
            "interactive OAuth authorization is required; authorize this connection "
            "through the FastAPI control plane first"
        )

    async with MCPConnection(
        config,
        oauth_storage=storage,
        oauth_redirect_handler=_reject_interactive_oauth,
        oauth_callback_handler=_reject_oauth_callback,
    ) as connection:
        return await connection.client.tools()


async def _reject_interactive_oauth(url: str) -> None:
    del url
    raise MCPSyncError(
        "stored OAuth credentials could not authorize the connection; "
        "interactive reauthorization is required"
    )


async def _reject_oauth_callback() -> tuple[str, str | None]:
    raise MCPSyncError("agent-mcp-sync cannot complete an interactive OAuth callback")


async def run_sync(settings: Settings) -> int:
    try:
        report = await sync_mcp_tools(settings)
    except (ImportError, OSError, ValueError, MCPSyncError) as exc:
        print(f"MCP sync error: {exc}", file=sys.stderr)
        return 1

    if not report.configured:
        print("No MCP configuration; nothing to sync.")
        return 0
    if not report.servers:
        print("No enabled MCP servers; nothing to sync.")
        return 0
    for server in report.servers:
        print(
            f"{server.connection_name}: synchronized {len(server.tool_names)} "
            f"Tool(s)"
        )
    print(
        f"Synchronized {len(report.servers)} MCP server(s) and "
        f"{report.tool_count} Tool(s)."
    )
    return 0


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        print(f"MCP sync error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    raise SystemExit(asyncio.run(run_sync(settings)))


if __name__ == "__main__":
    main()
