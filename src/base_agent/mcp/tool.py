"""Map MCP-discovered tools onto the base-agent Tool protocol."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema.validators import validator_for
from mcp.types import CallToolResult, ListToolsResult
from mcp.types import Tool as SDKTool

from base_agent.models import ToolDefinition
from base_agent.tools import (
    ToolConfirmationMode,
    ToolInvalidArgumentsError,
    ToolSideEffectMode,
)


class MCPSession(Protocol):
    """Narrow ClientSession surface consumed by this adapter."""

    async def list_tools(self, cursor: str | None, /) -> ListToolsResult: ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult: ...


class MCPToolCallError(RuntimeError):
    """An MCP server returned a Tool-level error result."""


class MCPTool:
    """One remotely executed MCP tool with local schema and permission checks."""

    def __init__(
        self,
        remote: SDKTool,
        session: MCPSession,
        *,
        name: str | None = None,
        permissions: frozenset[str] = frozenset({"mcp:invoke"}),
        timeout_seconds: float = 30.0,
        side_effect: ToolSideEffectMode = ToolSideEffectMode.UNSPECIFIED,
        confirmation: ToolConfirmationMode = ToolConfirmationMode.NONE,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be greater than zero")
        validator_type = validator_for(remote.inputSchema)
        validator_type.check_schema(remote.inputSchema)
        self._validator = validator_type(remote.inputSchema)
        self._remote_name = remote.name
        self._session = session
        self._definition = ToolDefinition(
            name=name or remote.name,
            description=remote.description or f"Execute MCP tool {remote.name}.",
            input_schema=remote.inputSchema,
        )
        self._permissions = permissions
        self._timeout_seconds = timeout_seconds
        if side_effect is ToolSideEffectMode.IDEMPOTENT:
            raise ValueError(
                "MCPTool cannot propagate a downstream idempotency key"
            )
        self._side_effect_mode = side_effect
        if (
            confirmation is ToolConfirmationMode.REQUIRED
            and side_effect
            not in {
                ToolSideEffectMode.UNSAFE,
                ToolSideEffectMode.IDEMPOTENT,
            }
        ):
            raise ValueError(
                "Tool confirmation requires an unsafe or idempotent side effect"
            )
        self._confirmation_mode = confirmation

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def remote_name(self) -> str:
        return self._remote_name

    @property
    def permissions(self) -> frozenset[str]:
        return self._permissions

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def side_effect_mode(self) -> ToolSideEffectMode:
        return self._side_effect_mode

    @property
    def confirmation_mode(self) -> ToolConfirmationMode:
        return self._confirmation_mode

    def validate_arguments(
        self,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        payload = dict(arguments)
        try:
            self._validator.validate(payload)
        except JsonSchemaValidationError as exc:
            location = ".".join(str(part) for part in exc.absolute_path)
            prefix = f"{location}: " if location else ""
            raise ToolInvalidArgumentsError(f"{prefix}{exc.message}") from exc
        return payload

    async def invoke(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(self.validate_arguments(arguments))

        result = await self._session.call_tool(self._remote_name, payload)
        if result.isError:
            message = _error_message(result)
            raise MCPToolCallError(message)
        return _result_payload(result)


def _error_message(result: CallToolResult) -> str:
    messages = [
        block.text
        for block in result.content
        if getattr(block, "type", None) == "text" and hasattr(block, "text")
    ]
    return "\n".join(messages) or "MCP server reported a tool execution error"


def _result_payload(result: CallToolResult) -> dict[str, Any]:
    content = [
        _strip_private_metadata(
            block.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        for block in result.content
    ]
    payload: dict[str, Any] = {"content": content}
    if result.structuredContent is not None:
        payload["structured_content"] = result.structuredContent
    return payload


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
