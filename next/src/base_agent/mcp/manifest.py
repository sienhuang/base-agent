"""Cached MCP Tool manifests and on-demand execution for process workers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ListToolsResult
from mcp.types import Tool as SDKTool

from base_agent.mcp.client import MCPClient
from base_agent.mcp.config import MCPAuthType, MCPServerConfig
from base_agent.mcp.connection import MCPConnection
from base_agent.mcp.storage import OAuthTokenStorage
from base_agent.mcp.tool import MCPTool
from base_agent.tools import ToolReadiness


class FileMCPToolManifestStore:
    """Atomic local manifest cache keyed by the complete server configuration."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    async def save(
        self,
        config: MCPServerConfig,
        tools: tuple[MCPTool, ...],
    ) -> None:
        payload: dict[str, object] = {
            "fingerprint": mcp_config_fingerprint(config),
            "tools": [
                tool.remote.model_dump(mode="json", by_alias=True, exclude_none=True)
                for tool in tools
            ],
        }
        await asyncio.to_thread(
            _atomic_write_json,
            self._path(config.name),
            payload,
        )

    def load(self, config: MCPServerConfig) -> tuple[SDKTool, ...]:
        path = self._path(config.name)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return ()
        if not isinstance(payload, dict):
            raise ValueError(f"MCP manifest '{path}' must contain an object")
        if payload.get("fingerprint") != mcp_config_fingerprint(config):
            return ()
        raw_tools = payload.get("tools")
        if not isinstance(raw_tools, list):
            raise ValueError(f"MCP manifest '{path}' has no Tool list")
        return tuple(SDKTool.model_validate(item) for item in raw_tools)

    def _path(self, connection_name: str) -> Path:
        if not connection_name or any(part in connection_name for part in ("/", "\\", "..")):
            raise ValueError("MCP connection name is not file-safe")
        return self._root / f"{connection_name}.json"


class MCPOnDemandSession:
    """Open an authenticated MCP connection for one operation, then close it."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        oauth_storage: OAuthTokenStorage | None = None,
    ) -> None:
        self.config = config
        self.oauth_storage = oauth_storage

    async def list_tools(self, cursor: str | None = None) -> ListToolsResult:
        async with MCPConnection(
            self.config,
            oauth_storage=self.oauth_storage,
        ) as connection:
            return await connection.client.session.list_tools(cursor=cursor)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        async with MCPConnection(
            self.config,
            oauth_storage=self.oauth_storage,
        ) as connection:
            return await connection.client.session.call_tool(name, arguments)

    async def readiness(self) -> ToolReadiness:
        if self.config.auth.type is not MCPAuthType.OAUTH:
            return ToolReadiness(ready=True)
        if self.oauth_storage is None:
            return ToolReadiness(
                ready=False,
                detail=f"MCP server '{self.config.name}' requires OAuth authorization",
            )
        tokens = await self.oauth_storage.get_tokens()
        expires_at = await self.oauth_storage.get_token_expiry_time()
        can_refresh = bool(
            tokens is not None
            and tokens.refresh_token
            and await self.oauth_storage.get_client_info()
        )
        if tokens is None or (
            expires_at is not None and time.time() >= expires_at and not can_refresh
        ):
            return ToolReadiness(
                ready=False,
                detail=f"MCP server '{self.config.name}' requires OAuth authorization",
            )
        return ToolReadiness(ready=True)


def tools_from_manifest(
    config: MCPServerConfig,
    manifest_store: FileMCPToolManifestStore,
    *,
    oauth_storage: OAuthTokenStorage | None = None,
) -> tuple[MCPTool, ...]:
    """Build process-safe Tools that authenticate when each operation runs."""

    remote_tools = manifest_store.load(config)
    if not remote_tools:
        return ()
    session = MCPOnDemandSession(config, oauth_storage=oauth_storage)
    return MCPClient(session, config, readiness=session.readiness).adapt_tools(remote_tools)


def mcp_config_fingerprint(config: MCPServerConfig) -> str:
    payload = config.model_dump(mode="json", exclude={"enabled"})
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, allow_nan=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
