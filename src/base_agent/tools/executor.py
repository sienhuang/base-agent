"""Permissioned and time-bounded tool execution."""

import asyncio
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from base_agent.models import ToolCall, ToolResult, ToolResultStatus, WaitForInput
from base_agent.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self,
        call: ToolCall,
        *,
        granted_permissions: frozenset[str] = frozenset(),
        allowed_tools: frozenset[str] | None = None,
    ) -> ToolResult:
        if allowed_tools is not None and call.name not in allowed_tools:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.DENIED,
                error_code="tool_not_allowed",
                message=f"tool '{call.name}' is not allowed in this execution",
            )

        registered_tool = self.registry.get(call.name)
        if registered_tool is None:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.NOT_FOUND,
                error_code="tool_not_found",
                message=f"tool '{call.name}' is not registered",
            )

        missing = registered_tool.permissions - granted_permissions
        if missing:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.DENIED,
                error_code="permission_denied",
                message=f"missing tool permissions: {', '.join(sorted(missing))}",
            )

        try:
            data = await asyncio.wait_for(
                registered_tool.invoke(_copy_arguments(call.arguments)),
                timeout=registered_tool.timeout_seconds,
            )
            if isinstance(data, WaitForInput):
                return ToolResult(
                    tool_name=call.name,
                    status=ToolResultStatus.WAITING,
                    data=data.model_dump(mode="json"),
                    message=data.prompt,
                )
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.SUCCESS,
                data=to_jsonable_python(data),
            )
        except ValidationError as exc:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.INVALID_ARGUMENTS,
                error_code="invalid_arguments",
                message=str(exc),
            )
        except TimeoutError:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.TIMEOUT,
                error_code="tool_timeout",
                message=f"tool exceeded timeout of {registered_tool.timeout_seconds:g} seconds",
            )
        except Exception as exc:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.ERROR,
                error_code="tool_execution_error",
                message=str(exc),
            )


def _copy_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return dict(arguments)
