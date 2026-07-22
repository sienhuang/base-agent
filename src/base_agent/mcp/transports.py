"""Explicit lifecycle helpers for official MCP client transports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from base_agent.mcp.client import MCPClient


@asynccontextmanager
async def stdio_mcp_client(
    parameters: StdioServerParameters,
    *,
    server_name: str | None = None,
    read_timeout_seconds: float | None = None,
) -> AsyncIterator[MCPClient]:
    """Start one configured stdio server and close it with the context."""

    timeout = _read_timeout(read_timeout_seconds)
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timeout,
        ) as session:
            initialized = await session.initialize()
            resolved_name = server_name or initialized.serverInfo.name
            yield MCPClient(session, server_name=resolved_name)


@asynccontextmanager
async def streamable_http_mcp_client(
    url: str,
    *,
    server_name: str | None = None,
    http_client: httpx.AsyncClient | None = None,
    terminate_on_close: bool = True,
    read_timeout_seconds: float | None = None,
) -> AsyncIterator[MCPClient]:
    """Connect to one Streamable HTTP MCP endpoint for the context lifetime."""

    timeout = _read_timeout(read_timeout_seconds)
    async with streamable_http_client(
        url,
        http_client=http_client,
        terminate_on_close=terminate_on_close,
    ) as (read_stream, write_stream, _):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timeout,
        ) as session:
            initialized = await session.initialize()
            resolved_name = server_name or initialized.serverInfo.name
            yield MCPClient(session, server_name=resolved_name)


def _read_timeout(seconds: float | None) -> timedelta | None:
    if seconds is not None and seconds <= 0:
        raise ValueError("read_timeout_seconds must be greater than zero")
    return timedelta(seconds=seconds) if seconds is not None else None
