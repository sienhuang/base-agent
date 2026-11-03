"""Loopback MCP bridge from application-owned Python Tools into DSH."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import uuid4

import uvicorn
from mcp import types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from dsh_base_agent.events import EventStore
from dsh_base_agent.governance import (
    Confirmation,
    ConfirmationPolicy,
    ConfirmationPolicyDecision,
    ConfirmationRequest,
    DenyConfirmations,
)
from dsh_base_agent.tools import Tool, ToolContext


class ToolGatewayError(RuntimeError):
    """The Tool gateway cannot safely expose or invoke a Tool."""


class ToolGateway:
    """Serve one Agent's selected Tools over loopback Streamable HTTP MCP.

    DSH owns tool-call scheduling.  This bridge keeps business Tool
    implementations, validation, readiness and governance in Python while DSH
    sees ordinary native MCP tools.
    """

    def __init__(
        self,
        *,
        agent_id: str,
        tools: Sequence[Tool],
        event_store: EventStore,
        confirmation_policy: ConfirmationPolicy | None = None,
        max_tool_calls: int = 3,
        max_tool_observation_bytes: int = 16_384,
        max_run_observation_bytes: int = 65_536,
    ) -> None:
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if max_tool_observation_bytes < 1:
            raise ValueError("max_tool_observation_bytes must be positive")
        if max_run_observation_bytes < max_tool_observation_bytes:
            raise ValueError("max_run_observation_bytes must cover one Tool observation")
        self.agent_id = agent_id
        self.tools = tuple(tools)
        self._tools_by_name = {tool.spec.name: tool for tool in self.tools}
        if len(self._tools_by_name) != len(self.tools):
            raise ValueError("Tool gateway requires unique Tool names")
        self._events = event_store
        self._confirmation_policy = confirmation_policy or DenyConfirmations()
        self._max_tool_calls = max_tool_calls
        self._max_tool_observation_bytes = max_tool_observation_bytes
        self._max_run_observation_bytes = max_run_observation_bytes
        self._active_run_id: str | None = None
        self._active_tool_calls = 0
        self._active_observation_bytes = 0
        self._server = Server(f"dsh-base-agent-{agent_id}", version="0.1.0")
        self._manager = StreamableHTTPSessionManager(
            self._server,
            json_response=True,
            stateless=True,
        )
        self._uvicorn: uvicorn.Server | None = None
        self._serve_task: asyncio.Task[None] | None = None
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(128)
        self._socket.setblocking(False)
        host, port = self._socket.getsockname()[:2]
        self.url = f"http://{host}:{port}/mcp"
        self._register_handlers()

    async def check_readiness(self) -> None:
        for tool in self.tools:
            readiness = await tool.check_readiness()
            if not readiness.ready:
                detail = f": {readiness.detail}" if readiness.detail else ""
                raise ToolGatewayError(f"Tool '{tool.spec.name}' is not ready{detail}")

    async def start(self) -> None:
        if self._serve_task is not None:
            return
        await self.check_readiness()
        server = uvicorn.Server(
            uvicorn.Config(
                self._manager.handle_request,
                log_level="warning",
                access_log=False,
                lifespan="off",
                interface="asgi3",
            )
        )
        self._uvicorn = server

        async def serve() -> None:
            # AnyIO cancel scopes must be entered and exited by the same Task.
            # Keeping the MCP manager around the Uvicorn lifetime also makes
            # real DSH shutdown deterministic.
            async with self._manager.run():
                await server.serve(sockets=[self._socket])

        self._serve_task = asyncio.create_task(
            serve(),
            name=f"dsh-tool-gateway-{self.agent_id}",
        )
        for _ in range(200):
            if server.started:
                return
            if self._serve_task.done():
                await self._serve_task
                raise ToolGatewayError("Tool gateway exited before startup")
            await asyncio.sleep(0.01)
        await self.close()
        raise TimeoutError("Tool gateway did not start within two seconds")

    def bind_run(self, run_id: str) -> None:
        if self._active_run_id is not None:
            raise ToolGatewayError("Tool gateway already belongs to an active Run")
        self._active_run_id = run_id
        self._active_tool_calls = 0
        self._active_observation_bytes = 0

    def release_run(self, run_id: str) -> None:
        if self._active_run_id == run_id:
            self._active_run_id = None
            self._active_tool_calls = 0
            self._active_observation_bytes = 0

    async def close(self) -> None:
        server = self._uvicorn
        task = self._serve_task
        self._uvicorn = None
        self._serve_task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            await task
        if self._socket.fileno() != -1:
            self._socket.close()

    def _register_handlers(self) -> None:
        @self._server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return [
                types.Tool(
                    name=tool.spec.name,
                    description=tool.spec.description,
                    inputSchema=dict(tool.spec.input_schema),
                )
                for tool in self.tools
            ]

        @self._server.call_tool()  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            run_id = self._active_run_id
            if run_id is None:
                raise ToolGatewayError("no active Agent Run owns this Tool call")
            try:
                tool = self._tools_by_name[name]
            except KeyError as exc:
                raise ToolGatewayError(f"Tool '{name}' is not registered") from exc
            validated = dict(tool.validate_arguments(arguments))
            tool_call_id = f"mcp-{uuid4().hex}"
            operation_id = f"op:{run_id}:{tool_call_id}"
            intent = _json_digest({"tool": name, "arguments": validated})
            await self._events.emit(
                run_id,
                "tool.started",
                {
                    "tool_name": name,
                    "tool_call_id": tool_call_id,
                    "operation_id": operation_id,
                    "intent_sha256": intent,
                    "runtime": "dsh-mcp",
                },
            )
            try:
                self._active_tool_calls += 1
                if self._active_tool_calls > self._max_tool_calls:
                    raise ToolGatewayError(f"Tool call budget exceeded ({self._max_tool_calls})")
                await self._authorize(tool, run_id, tool_call_id, validated)
                result = await asyncio.wait_for(
                    tool.invoke(
                        validated,
                        ToolContext(
                            run_id=run_id,
                            agent_id=self.agent_id,
                            tool_call_id=tool_call_id,
                            operation_id=operation_id,
                        ),
                    ),
                    timeout=tool.timeout_seconds,
                )
                normalized = _json_value(result)
                observation_bytes = len(
                    json.dumps(
                        normalized,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                )
                if observation_bytes > self._max_tool_observation_bytes:
                    raise ToolGatewayError("Tool observation exceeds max_tool_observation_bytes")
                self._active_observation_bytes += observation_bytes
                if self._active_observation_bytes > self._max_run_observation_bytes:
                    raise ToolGatewayError("Tool observations exceed max_run_observation_bytes")
            except BaseException as exc:
                await self._events.emit(
                    run_id,
                    "tool.completed",
                    {
                        "tool_name": name,
                        "tool_call_id": tool_call_id,
                        "operation_id": operation_id,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "runtime": "dsh-mcp",
                    },
                )
                raise
            await self._events.emit(
                run_id,
                "tool.completed",
                {
                    "tool_name": name,
                    "tool_call_id": tool_call_id,
                    "operation_id": operation_id,
                    "status": "success",
                    "result_sha256": _json_digest(normalized),
                    "runtime": "dsh-mcp",
                },
            )
            return normalized if isinstance(normalized, dict) else {"result": normalized}

    async def _authorize(
        self,
        tool: Tool,
        run_id: str,
        tool_call_id: str,
        arguments: Mapping[str, Any],
    ) -> None:
        if tool.confirmation is Confirmation.NONE:
            return
        decision = await self._confirmation_policy.confirm(
            ConfirmationRequest(
                run_id=run_id,
                agent_id=self.agent_id,
                tool_name=tool.spec.name,
                arguments=arguments,
                side_effect=tool.side_effect,
                tool_call_id=tool_call_id,
            )
        )
        if decision is True or decision is ConfirmationPolicyDecision.APPROVE:
            return
        if decision is ConfirmationPolicyDecision.DEFER:
            raise ToolGatewayError(
                "DSH MCP Tools cannot suspend for confirmation in this prototype"
            )
        raise ToolGatewayError(f"Tool '{tool.spec.name}' was denied by confirmation policy")


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ToolGatewayError("Tool result must be finite JSON") from exc


def _json_digest(value: Any) -> str:
    serialized = json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode()).hexdigest()


__all__ = ["ToolGateway", "ToolGatewayError"]
