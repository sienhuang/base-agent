"""Explicit MCP transport and OAuth-aware connection lifecycle."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any, cast

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.auth import OAuthClientProvider
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import OAuthClientMetadata

from base_agent.mcp.client import MCPClient, MCPDiscoverySession
from base_agent.mcp.config import MCPAuthType, MCPServerConfig, MCPTransport
from base_agent.mcp.storage import OAuthTokenStorage
from base_agent.tools import ToolReadiness

OAuthRedirectHandler = Callable[[str], Awaitable[None]]
OAuthCallbackHandler = Callable[[], Awaitable[tuple[str, str | None]]]


class PersistentOAuthClientProvider(OAuthClientProvider):
    """Restore persisted expiry so an expired token refreshes after restart."""

    def __init__(
        self,
        server_url: str,
        client_metadata: OAuthClientMetadata,
        *,
        storage: OAuthTokenStorage,
        redirect_handler: OAuthRedirectHandler | None = None,
        callback_handler: OAuthCallbackHandler | None = None,
        timeout: float = 300.0,
        client_metadata_url: str | None = None,
    ) -> None:
        self._persistent_storage = storage
        super().__init__(
            server_url,
            client_metadata,
            storage,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=timeout,
            client_metadata_url=client_metadata_url,
        )

    async def _initialize(self) -> None:
        await super()._initialize()
        self.context.token_expiry_time = (
            await self._persistent_storage.get_token_expiry_time()
        )


class MCPConnection:
    """Own one MCP transport, ClientSession, and its discovered Tool client."""

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        oauth_storage: OAuthTokenStorage | None = None,
        oauth_redirect_handler: OAuthRedirectHandler | None = None,
        oauth_callback_handler: OAuthCallbackHandler | None = None,
    ) -> None:
        self.config = config
        self._oauth_storage = oauth_storage
        self._oauth_redirect_handler = oauth_redirect_handler
        self._oauth_callback_handler = oauth_callback_handler
        self._stack: AsyncExitStack | None = None
        self._owner_task: asyncio.Task[Any] | None = None
        self._session: ClientSession | None = None
        self._client: MCPClient | None = None
        self._server_info_name: str | None = None

    @property
    def client(self) -> MCPClient:
        if self._client is None:
            raise RuntimeError(f"MCP server '{self.config.name}' is not connected")
        return self._client

    @property
    def server_info_name(self) -> str | None:
        return self._server_info_name

    async def __aenter__(self) -> MCPConnection:
        await self.open()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._stack is not None:
            return
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("MCP connection requires a running asyncio Task")
        stack = AsyncExitStack()
        self._owner_task = owner
        self._stack = stack
        try:
            async with asyncio.timeout(self.config.connect_timeout_seconds):
                read_stream, write_stream = await self._open_transport(stack)
                timeout = (
                    timedelta(seconds=self.config.read_timeout_seconds)
                    if self.config.read_timeout_seconds is not None
                    else None
                )
                session = await stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timeout,
                    )
                )
                initialized = await session.initialize()
            self._session = session
            self._server_info_name = initialized.serverInfo.name
            self._client = MCPClient(
                cast(MCPDiscoverySession, session),
                self.config,
                readiness=self.readiness,
            )
        except BaseException:
            await stack.aclose()
            self._clear()
            raise

    async def close(self) -> None:
        stack = self._stack
        if stack is None:
            return
        if asyncio.current_task() is not self._owner_task:
            raise RuntimeError(
                "MCP connection must close in the same asyncio Task that opened it"
            )
        try:
            await stack.aclose()
        finally:
            self._clear()

    def readiness(self) -> ToolReadiness:
        if self._session is None:
            return ToolReadiness(
                ready=False,
                detail=f"MCP server '{self.config.name}' is not connected",
            )
        return ToolReadiness(ready=True)

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.transport is MCPTransport.STDIO:
            assert self.config.command is not None
            parameters = StdioServerParameters(
                command=self.config.command,
                args=list(self.config.args),
                cwd=self.config.cwd,
                env={**os.environ, **self.config.env},
                encoding_error_handler="replace",
            )
            return await stack.enter_async_context(stdio_client(parameters))

        assert self.config.url is not None
        http_client = httpx.AsyncClient(
            headers=self._http_headers(),
            auth=self._http_auth(),
            follow_redirects=True,
            timeout=httpx.Timeout(
                self.config.connect_timeout_seconds,
                read=self.config.read_timeout_seconds or 300.0,
            ),
        )
        await stack.enter_async_context(http_client)
        read_stream, write_stream, _ = await stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=http_client)
        )
        return read_stream, write_stream

    def _http_headers(self) -> dict[str, str] | None:
        auth = self.config.auth
        headers = dict(self.config.headers)
        if auth.type is MCPAuthType.HEADERS:
            headers.update(auth.headers)
            return headers
        if auth.type is MCPAuthType.BEARER:
            assert auth.token_env is not None
            token = os.getenv(auth.token_env)
            if token is None or not token.strip():
                raise ValueError(
                    f"environment variable '{auth.token_env}' is required for "
                    f"MCP server '{self.config.name}'"
                )
            headers["Authorization"] = f"Bearer {token.strip()}"
            return headers
        return headers or None

    def _http_auth(self) -> httpx.Auth | None:
        auth = self.config.auth
        if auth.type is not MCPAuthType.OAUTH:
            return None
        if self._oauth_storage is None:
            raise ValueError(
                f"OAuth storage is required for MCP server '{self.config.name}'"
            )
        assert self.config.url is not None
        assert auth.redirect_uri is not None
        metadata = OAuthClientMetadata.model_validate(
            {
                "redirect_uris": [auth.redirect_uri],
                "scope": " ".join(auth.scopes) or None,
                "client_name": auth.client_name,
                "client_uri": auth.client_uri,
            }
        )
        return PersistentOAuthClientProvider(
            self.config.url,
            metadata,
            storage=self._oauth_storage,
            redirect_handler=self._oauth_redirect_handler,
            callback_handler=self._oauth_callback_handler,
            timeout=auth.authorization_timeout_seconds,
            client_metadata_url=auth.client_metadata_url,
        )

    def _clear(self) -> None:
        self._stack = None
        self._owner_task = None
        self._session = None
        self._client = None
        self._server_info_name = None
