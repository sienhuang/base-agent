"""Adapt one remotely discovered MCP Tool to the base-agent Tool protocol."""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import re
from collections.abc import Awaitable, Callable, Mapping
from pathlib import PurePosixPath
from typing import Any, Protocol

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult
from mcp.types import Tool as SDKTool

from base_agent.governance import Confirmation, SideEffect
from base_agent.models import ToolSpec
from base_agent.tools import ToolContext, ToolReadiness


class MCPSession(Protocol):
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult: ...


class MCPToolCallError(RuntimeError):
    """The remote MCP server returned a Tool-level error result."""


Readiness = Callable[[], ToolReadiness | Awaitable[ToolReadiness]]


class MCPTool:
    """One remote MCP Tool sharing a connection owned by its host runtime."""

    def __init__(
        self,
        remote: SDKTool,
        session: MCPSession,
        *,
        name: str,
        server_name: str,
        permissions: frozenset[str],
        timeout_seconds: float = 30.0,
        side_effect: SideEffect = SideEffect.UNKNOWN,
        confirmation: Confirmation = Confirmation.NONE,
        readiness: Readiness | None = None,
        call_lock: asyncio.Lock | None = None,
        max_artifact_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("MCP Tool timeout_seconds must be positive")
        if max_artifact_bytes < 1:
            raise ValueError("MCP Tool max_artifact_bytes must be positive")
        if confirmation is Confirmation.REQUIRED and side_effect is SideEffect.READ_ONLY:
            raise ValueError("read-only MCP Tools must not require confirmation")
        validator_type = validator_for(remote.inputSchema)
        validator_type.check_schema(remote.inputSchema)
        self._validator = validator_type(remote.inputSchema)
        self._remote = remote
        self._session = session
        self._server_name = server_name
        self._spec = ToolSpec(
            name=name,
            description=remote.description or f"MCP Tool {remote.name} from {server_name}.",
            input_schema=remote.inputSchema,
        )
        self._permissions = permissions
        self._timeout_seconds = timeout_seconds
        self._side_effect = side_effect
        self._confirmation = confirmation
        self._readiness = readiness
        self._call_lock = call_lock
        self._max_artifact_bytes = max_artifact_bytes

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    @property
    def remote_name(self) -> str:
        return self._remote.name

    @property
    def remote(self) -> SDKTool:
        return self._remote

    @property
    def server_name(self) -> str:
        return self._server_name

    @property
    def permissions(self) -> frozenset[str]:
        return self._permissions

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def side_effect(self) -> SideEffect:
        return self._side_effect

    @property
    def confirmation(self) -> Confirmation:
        return self._confirmation

    def validate_arguments(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = dict(arguments)
        try:
            self._validator.validate(payload)
        except JsonSchemaValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path)
            prefix = f"{location}: " if location else ""
            raise ValueError(f"{prefix}{exc.message}") from exc
        return payload

    async def check_readiness(self) -> ToolReadiness:
        if self._readiness is None:
            return ToolReadiness(ready=True)
        value = self._readiness()
        return await value if isinstance(value, Awaitable) else value

    async def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> Any:
        payload = dict(self.validate_arguments(arguments))
        if self._call_lock is None:
            result = await self._session.call_tool(self.remote_name, payload)
        else:
            async with self._call_lock:
                result = await self._session.call_tool(self.remote_name, payload)
        if result.isError:
            raise MCPToolCallError(_error_message(result))
        return await _result_payload(
            result,
            context=context,
            server_name=self.server_name,
            tool_name=self.remote_name,
            max_artifact_bytes=self._max_artifact_bytes,
        )


async def _result_payload(
    result: CallToolResult,
    *,
    context: ToolContext,
    server_name: str,
    tool_name: str,
    max_artifact_bytes: int,
) -> dict[str, Any]:
    content = []
    for index, block in enumerate(result.content):
        content.append(
            await _content_payload(
                block,
                index=index,
                context=context,
                server_name=server_name,
                tool_name=tool_name,
                max_artifact_bytes=max_artifact_bytes,
            )
        )
    payload: dict[str, Any] = {"content": content}
    if result.structuredContent is not None:
        payload["structured_content"] = _strip_private_metadata(result.structuredContent)
    return payload


async def _content_payload(
    block: Any,
    *,
    index: int,
    context: ToolContext,
    server_name: str,
    tool_name: str,
    max_artifact_bytes: int,
) -> dict[str, Any]:
    block_type = str(getattr(block, "type", ""))
    data = getattr(block, "data", None)
    media_type = getattr(block, "mimeType", None)
    if block_type in {"image", "audio"} and isinstance(data, str) and media_type:
        artifact = await _create_binary_artifact(
            data,
            media_type=str(media_type),
            suggested_name=f"{server_name}-{tool_name}-{index}",
            context=context,
            max_artifact_bytes=max_artifact_bytes,
            block_type=block_type,
        )
        if artifact is not None:
            return {"type": block_type, "artifact": artifact}

    resource = getattr(block, "resource", None)
    blob = getattr(resource, "blob", None)
    resource_media_type = getattr(resource, "mimeType", None)
    if block_type == "resource" and isinstance(blob, str) and resource_media_type:
        uri = str(getattr(resource, "uri", ""))
        suggested_name = PurePosixPath(uri).name or f"{server_name}-{tool_name}-{index}"
        artifact = await _create_binary_artifact(
            blob,
            media_type=str(resource_media_type),
            suggested_name=suggested_name,
            context=context,
            max_artifact_bytes=max_artifact_bytes,
            block_type="resource",
            location=uri or None,
        )
        if artifact is not None:
            return {"type": "resource", "uri": uri, "artifact": artifact}

    payload = _strip_private_metadata(
        block.model_dump(mode="json", by_alias=True, exclude_none=True)
    )
    if not isinstance(payload, dict):
        raise TypeError("MCP content block must serialize to an object")
    return payload


async def _create_binary_artifact(
    encoded: str,
    *,
    media_type: str,
    suggested_name: str,
    context: ToolContext,
    max_artifact_bytes: int,
    block_type: str,
    location: str | None = None,
) -> dict[str, Any] | None:
    if context.artifacts is None:
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise MCPToolCallError(f"MCP {block_type} content is not valid base64") from exc
    name = _artifact_name(suggested_name, media_type)
    artifact = await context.artifacts.create(
        name=name,
        media_type=media_type,
        content=content,
        location=location,
        metadata={"source": "mcp", "content_type": block_type},
        max_bytes=max_artifact_bytes,
    )
    return {
        "id": artifact.id,
        "name": artifact.name,
        "media_type": artifact.media_type,
        "size_bytes": artifact.size_bytes,
        "sha256": artifact.sha256,
    }


def _artifact_name(value: str, media_type: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-") or "mcp-content"
    if "." not in PurePosixPath(name).name:
        extension = mimetypes.guess_extension(media_type.split(";", 1)[0].strip())
        if extension:
            name += extension
    return name[:200]


def _error_message(result: CallToolResult) -> str:
    messages: list[str] = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            messages.append(str(text))
            continue
        resource_text = getattr(getattr(block, "resource", None), "text", None)
        if resource_text:
            messages.append(str(resource_text))
    return "\n".join(messages) or "MCP server reported a Tool execution error"


def _strip_private_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_private_metadata(item)
            for key, item in value.items()
            if key != "_meta"
        }
    if isinstance(value, list):
        return [_strip_private_metadata(item) for item in value]
    return value
