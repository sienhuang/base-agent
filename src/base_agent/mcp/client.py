"""MCP discovery and Tool construction."""

from __future__ import annotations

from collections.abc import Mapping

from base_agent.mcp.tool import MCPSession, MCPTool


class MCPClient:
    """Discover a stable snapshot of tools from one initialized MCP session."""

    def __init__(self, session: MCPSession, *, server_name: str) -> None:
        if not server_name.strip():
            raise ValueError("server_name must not be blank")
        self.session = session
        self.server_name = server_name

    async def tools(
        self,
        *,
        name_prefix: str | None = None,
        permissions: frozenset[str] = frozenset({"mcp:invoke"}),
        permissions_by_tool: Mapping[str, frozenset[str]] | None = None,
        timeout_seconds: float = 30.0,
    ) -> tuple[MCPTool, ...]:
        if name_prefix is not None and not name_prefix:
            raise ValueError("name_prefix must not be empty")
        discovered = []
        cursor: str | None = None
        while True:
            page = await self.session.list_tools(cursor)
            discovered.extend(page.tools)
            cursor = page.nextCursor
            if cursor is None:
                break

        remote_names = [tool.name for tool in discovered]
        if len(set(remote_names)) != len(remote_names):
            raise ValueError(f"MCP server '{self.server_name}' returned duplicate tool names")
        overrides = permissions_by_tool or {}
        unknown_overrides = set(overrides) - set(remote_names)
        if unknown_overrides:
            names = ", ".join(sorted(unknown_overrides))
            raise ValueError(f"permissions configured for unknown MCP tools: {names}")

        return tuple(
            MCPTool(
                remote,
                self.session,
                name=f"{name_prefix}.{remote.name}" if name_prefix else remote.name,
                permissions=overrides.get(remote.name, permissions),
                timeout_seconds=timeout_seconds,
            )
            for remote in discovered
        )
