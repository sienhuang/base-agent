"""Paginated MCP Tool discovery over one initialized ClientSession."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from typing import Protocol

from mcp.types import ListToolsResult
from mcp.types import Tool as SDKTool

from base_agent.governance import SideEffect
from base_agent.mcp.config import MCPServerConfig
from base_agent.mcp.tool import MCPSession, MCPTool
from base_agent.tools import ToolReadiness


class MCPDiscoverySession(MCPSession, Protocol):
    async def list_tools(self, cursor: str | None = None) -> ListToolsResult: ...


Readiness = Callable[[], ToolReadiness | Awaitable[ToolReadiness]]


class MCPClient:
    """Discover a stable local Tool snapshot from one MCP server."""

    def __init__(
        self,
        session: MCPDiscoverySession,
        config: MCPServerConfig,
        *,
        readiness: Readiness | None = None,
    ) -> None:
        self.session = session
        self.config = config
        self._readiness = readiness
        self._call_lock = asyncio.Lock()

    async def tools(self) -> tuple[MCPTool, ...]:
        discovered = []
        cursor: str | None = None
        while True:
            page = await self.session.list_tools(cursor=cursor)
            discovered.extend(page.tools)
            cursor = page.nextCursor
            if cursor is None:
                break

        return self.adapt_tools(tuple(discovered))

    def adapt_tools(self, discovered: tuple[SDKTool, ...]) -> tuple[MCPTool, ...]:
        """Adapt an already discovered or cached immutable Tool list."""

        remote_names = [tool.name for tool in discovered]
        if len(remote_names) != len(set(remote_names)):
            raise ValueError(
                f"MCP server '{self.config.name}' returned duplicate Tool names"
            )
        unknown_policies = set(self.config.tools) - set(remote_names)
        if unknown_policies:
            names = ", ".join(sorted(unknown_policies))
            raise ValueError(f"policies configured for unknown MCP Tools: {names}")

        local_names = [mcp_tool_name(self.config.name, name) for name in remote_names]
        if len(local_names) != len(set(local_names)):
            raise ValueError(
                f"MCP server '{self.config.name}' has Tool names that collide after normalization"
            )

        tools = []
        for remote, local_name in zip(discovered, local_names, strict=True):
            policy = self.config.policy_for(remote.name)
            side_effect = policy.side_effect or _default_side_effect(remote, self.config)
            tools.append(
                MCPTool(
                    remote,
                    self.session,
                    name=local_name,
                    server_name=self.config.name,
                    permissions=(
                        policy.permissions
                        if policy.permissions is not None
                        else self.config.resolved_default_permissions
                    ),
                    timeout_seconds=policy.timeout_seconds or self.config.tool_timeout_seconds,
                    side_effect=side_effect,
                    confirmation=policy.confirmation or self.config.default_confirmation,
                    readiness=self._readiness,
                    call_lock=(
                        None
                        if self.config.supports_parallel_tool_calls
                        else self._call_lock
                    ),
                    max_artifact_bytes=self.config.max_artifact_bytes,
                )
            )
        return tuple(tools)


def mcp_tool_name(server_name: str, remote_tool_name: str) -> str:
    """Return a provider-safe, namespaced MCP Tool name."""

    server = _safe_name_component(server_name)
    tool = _safe_name_component(remote_tool_name)
    candidate = f"mcp__{server}__{tool}"
    if len(candidate) <= 64:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]
    return f"{candidate[:52]}__{digest}"


def _safe_name_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if re.search(r"[A-Za-z0-9]", normalized):
        return normalized
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"tool_{digest}"


def _default_side_effect(remote: object, config: MCPServerConfig) -> SideEffect:
    if config.trust_read_only_annotations:
        annotations = getattr(remote, "annotations", None)
        if getattr(annotations, "readOnlyHint", False) is True:
            return SideEffect.READ_ONLY
    return config.default_side_effect
