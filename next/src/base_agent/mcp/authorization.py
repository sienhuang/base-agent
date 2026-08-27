"""Coordinate browser OAuth without making an execution Worker interactive."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import parse_qs, urlparse

from base_agent.mcp.config import MCPAuthType, MCPServerConfig
from base_agent.mcp.connection import MCPConnection
from base_agent.mcp.storage import OAuthTokenStorage
from base_agent.mcp.tool import MCPTool

StorageFactory = Callable[[MCPServerConfig], OAuthTokenStorage | None]
AuthorizedHook = Callable[[MCPServerConfig, tuple[MCPTool, ...]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class MCPAuthorizationStart:
    connection_name: str
    status: Literal["authorized", "authorization_required"]
    authorization_url: str | None = None


@dataclass(frozen=True, slots=True)
class MCPAuthorizationComplete:
    connection_name: str
    tools: tuple[MCPTool, ...]


@dataclass(slots=True)
class _AuthorizationFlow:
    config: MCPServerConfig
    callback: asyncio.Future[tuple[str, str | None]]
    redirect_ready: asyncio.Event = field(default_factory=asyncio.Event)
    authorization_url: str | None = None
    state: str | None = None
    task: asyncio.Task[tuple[MCPTool, ...]] | None = None


class MCPAuthorizationManager:
    """Own pending OAuth browser handshakes for configured MCP connections."""

    def __init__(
        self,
        configs: Mapping[str, MCPServerConfig],
        *,
        storage_factory: StorageFactory,
        on_authorized: AuthorizedHook | None = None,
    ) -> None:
        self._configs = dict(configs)
        self._storage_factory = storage_factory
        self._on_authorized = on_authorized
        self._by_connection: dict[str, _AuthorizationFlow] = {}
        self._by_state: dict[str, _AuthorizationFlow] = {}
        self._lock = asyncio.Lock()

    async def start(self, connection_name: str) -> MCPAuthorizationStart:
        config = self._require_config(connection_name)
        async with self._lock:
            current = self._by_connection.get(connection_name)
            if current is not None and current.task is not None and not current.task.done():
                if current.authorization_url is not None:
                    return MCPAuthorizationStart(
                        connection_name,
                        "authorization_required",
                        current.authorization_url,
                    )
                flow = current
            else:
                loop = asyncio.get_running_loop()
                flow = _AuthorizationFlow(
                    config=config,
                    callback=loop.create_future(),
                )
                self._by_connection[connection_name] = flow
                flow.task = asyncio.create_task(
                    self._run(flow),
                    name=f"mcp-authorize:{connection_name}",
                )

        assert flow.task is not None
        redirect_wait = asyncio.create_task(flow.redirect_ready.wait())
        try:
            done, _ = await asyncio.wait(
                {flow.task, redirect_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if flow.task in done:
                await flow.task
                return MCPAuthorizationStart(connection_name, "authorized")
            assert flow.authorization_url is not None
            return MCPAuthorizationStart(
                connection_name,
                "authorization_required",
                flow.authorization_url,
            )
        finally:
            if not redirect_wait.done():
                redirect_wait.cancel()
                await asyncio.gather(redirect_wait, return_exceptions=True)

    async def complete(
        self,
        *,
        code: str | None,
        state: str,
        error: str | None = None,
    ) -> MCPAuthorizationComplete:
        async with self._lock:
            flow = self._by_state.get(state)
        if flow is None:
            raise ValueError("unknown or expired MCP OAuth state")
        if flow.callback.done():
            raise ValueError("MCP OAuth callback was already consumed")
        if error is not None:
            flow.callback.set_exception(RuntimeError(f"MCP OAuth authorization failed: {error}"))
        elif code is None or not code.strip():
            flow.callback.set_exception(ValueError("MCP OAuth callback has no code"))
        else:
            flow.callback.set_result((code, state))
        assert flow.task is not None
        tools = await flow.task
        return MCPAuthorizationComplete(flow.config.name, tools)

    async def token_available(self, connection_name: str) -> bool:
        config = self._require_config(connection_name)
        if config.auth.type is not MCPAuthType.OAUTH:
            return True
        storage = self._storage_factory(config)
        if storage is None:
            return False
        tokens = await storage.get_tokens()
        if tokens is None:
            return False
        expires_at = await storage.get_token_expiry_time()
        if expires_at is None or time.time() < expires_at:
            return True
        return bool(tokens.refresh_token and await storage.get_client_info())

    async def close(self) -> None:
        async with self._lock:
            tasks = [
                flow.task
                for flow in self._by_connection.values()
                if flow.task is not None and not flow.task.done()
            ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, flow: _AuthorizationFlow) -> tuple[MCPTool, ...]:
        storage = self._storage_factory(flow.config)

        async def redirect_handler(url: str) -> None:
            state = _authorization_state(url)
            async with self._lock:
                existing = self._by_state.get(state)
                if existing is not None and existing is not flow:
                    raise RuntimeError("duplicate MCP OAuth state")
                flow.authorization_url = url
                flow.state = state
                self._by_state[state] = flow
                flow.redirect_ready.set()

        async def callback_handler() -> tuple[str, str | None]:
            return await flow.callback

        try:
            async with MCPConnection(
                flow.config,
                oauth_storage=storage,
                oauth_redirect_handler=redirect_handler,
                oauth_callback_handler=callback_handler,
            ) as connection:
                tools = await connection.client.tools()
                if self._on_authorized is not None:
                    await self._on_authorized(flow.config, tools)
                return tools
        finally:
            async with self._lock:
                if self._by_connection.get(flow.config.name) is flow:
                    self._by_connection.pop(flow.config.name, None)
                if flow.state is not None and self._by_state.get(flow.state) is flow:
                    self._by_state.pop(flow.state, None)

    def _require_config(self, connection_name: str) -> MCPServerConfig:
        try:
            return self._configs[connection_name]
        except KeyError as exc:
            raise ValueError(f"unknown MCP connection '{connection_name}'") from exc


def _authorization_state(url: str) -> str:
    values = parse_qs(urlparse(url).query).get("state")
    if not values or not values[0]:
        raise ValueError("MCP OAuth authorization URL has no state")
    return values[0]
