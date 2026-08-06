"""Permissioned and time-bounded tool execution."""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from base_agent.models import (
    ToolCall,
    ToolConfirmationDecision,
    ToolResult,
    ToolResultStatus,
    WaitForInput,
)
from base_agent.tools.context import ToolContext
from base_agent.tools.effects import (
    ConfirmableTool,
    DeclaredToolConfirmationPolicy,
    GovernedTool,
    ToolConfirmationMode,
    ToolConfirmationPolicy,
    ToolSideEffectContextError,
    ToolSideEffectMode,
    ToolSideEffectRecorder,
    ToolSideEffectRecorderError,
    ToolSideEffectReplayUnsafeError,
)
from base_agent.tools.errors import ToolInvalidArgumentsError
from base_agent.tools.protocol import ArgumentValidatingTool, ContextualTool
from base_agent.tools.registry import ToolRegistry
from base_agent.tools.results import (
    BoundedToolResultPolicy,
    ToolResultPolicy,
)


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        side_effect_recorder: ToolSideEffectRecorder | None = None,
        confirmation_policy: ToolConfirmationPolicy | None = None,
        result_policy: ToolResultPolicy | None = None,
    ) -> None:
        self.registry = registry
        self.side_effect_recorder = side_effect_recorder
        self.confirmation_policy = (
            confirmation_policy or DeclaredToolConfirmationPolicy()
        )
        self.result_policy = result_policy or BoundedToolResultPolicy()

    async def execute(
        self,
        call: ToolCall,
        *,
        granted_permissions: frozenset[str] = frozenset(),
        allowed_tools: frozenset[str] | None = None,
        context: ToolContext | None = None,
    ) -> ToolResult:
        result = await self._execute_unbounded(
            call,
            granted_permissions=granted_permissions,
            allowed_tools=allowed_tools,
            context=context,
        )
        return self.result_policy.enforce(result)

    async def _execute_unbounded(
        self,
        call: ToolCall,
        *,
        granted_permissions: frozenset[str] = frozenset(),
        allowed_tools: frozenset[str] | None = None,
        context: ToolContext | None = None,
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
            arguments = _copy_arguments(call.arguments)
            if isinstance(registered_tool, ArgumentValidatingTool):
                arguments = dict(
                    registered_tool.validate_arguments(arguments)
                )
            execution_context = (
                replace(context, tool_call_id=call.id)
                if context is not None
                else None
            )
            receipt = None
            recorder = self.side_effect_recorder
            side_effect_mode = (
                registered_tool.side_effect_mode
                if isinstance(registered_tool, GovernedTool)
                else ToolSideEffectMode.UNSPECIFIED
            )
            confirmation_mode = (
                registered_tool.confirmation_mode
                if isinstance(registered_tool, ConfirmableTool)
                else ToolConfirmationMode.NONE
            )
            if (
                execution_context is None
                and confirmation_mode is ToolConfirmationMode.REQUIRED
            ):
                return ToolResult(
                    tool_name=call.name,
                    status=ToolResultStatus.ERROR,
                    error_code="tool_confirmation_context_required",
                    message="Tool confirmation requires execution context",
                )
            confirmation_request = (
                await self.confirmation_policy.request(
                    call,
                    tool_name=call.name,
                    side_effect_mode=side_effect_mode,
                    confirmation_mode=confirmation_mode,
                    context=execution_context,
                )
                if execution_context is not None
                else None
            )
            if confirmation_request is not None:
                assert execution_context is not None
                confirmation = execution_context.confirmation
                if confirmation is None:
                    wait = WaitForInput(
                        prompt=confirmation_request.prompt,
                        metadata={
                            "kind": "tool_confirmation",
                            "request": confirmation_request.model_dump(
                                mode="json"
                            ),
                        },
                    )
                    return ToolResult(
                        tool_name=call.name,
                        status=ToolResultStatus.WAITING,
                        data=wait.model_dump(mode="json"),
                        message=wait.prompt,
                    )
                if confirmation.request_id != confirmation_request.id:
                    return ToolResult(
                        tool_name=call.name,
                        status=ToolResultStatus.DENIED,
                        error_code="tool_confirmation_mismatch",
                        message="Tool confirmation targets another request",
                    )
                if (
                    confirmation.decision
                    is ToolConfirmationDecision.REJECT
                ):
                    return ToolResult(
                        tool_name=call.name,
                        status=ToolResultStatus.DENIED,
                        error_code="tool_confirmation_rejected",
                        message="Tool execution was rejected",
                    )
            if (
                recorder is not None
                and side_effect_mode
                in {
                    ToolSideEffectMode.UNSAFE,
                    ToolSideEffectMode.IDEMPOTENT,
                }
            ):
                if execution_context is None:
                    raise ToolSideEffectContextError(
                        "governed Tool execution requires ToolContext"
                    )
                if (
                    side_effect_mode is ToolSideEffectMode.IDEMPOTENT
                    and not isinstance(registered_tool, ContextualTool)
                ):
                    raise ToolSideEffectContextError(
                        "idempotent Tool must consume ToolContext"
                    )
                receipt = await recorder.start(
                    call,
                    tool_name=call.name,
                    mode=side_effect_mode,
                    context=execution_context,
                )
                execution_context = replace(
                    execution_context,
                    idempotency_key=receipt.idempotency_key,
                )
            invocation = (
                registered_tool.invoke_with_context(
                    arguments,
                    execution_context,
                )
                if execution_context is not None
                and isinstance(registered_tool, ContextualTool)
                else registered_tool.invoke(arguments)
            )
            async with asyncio.timeout(registered_tool.timeout_seconds):
                data = await invocation
            if isinstance(data, WaitForInput):
                return ToolResult(
                    tool_name=call.name,
                    status=ToolResultStatus.WAITING,
                    data=data.model_dump(mode="json"),
                    message=data.prompt,
                )
            if receipt is not None:
                assert recorder is not None
                await recorder.confirm(receipt)
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.SUCCESS,
                data=to_jsonable_python(data),
            )
        except (ValidationError, ToolInvalidArgumentsError) as exc:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.INVALID_ARGUMENTS,
                error_code="invalid_arguments",
                message=str(exc),
            )
        except ToolSideEffectReplayUnsafeError:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.DENIED,
                error_code="side_effect_replay_unsafe",
                message="Tool replay requires operator review",
            )
        except ToolSideEffectRecorderError:
            return ToolResult(
                tool_name=call.name,
                status=ToolResultStatus.ERROR,
                error_code="side_effect_evidence_error",
                message="Tool side-effect evidence could not be recorded",
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
