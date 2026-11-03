"""Typed business Tools exposed to DSH through the local MCP gateway."""

from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast, get_type_hints, overload, runtime_checkable

from pydantic import BaseModel, ConfigDict, create_model

from dsh_base_agent.governance import (
    AllowAllConfirmations,
    AwaitHumanConfirmations,
    Confirmation,
    ConfirmationPolicy,
    ConfirmationPolicyDecision,
    DenyConfirmations,
    SideEffect,
    ToolConfirmation,
    ToolConfirmationDecision,
)
from dsh_base_agent.models import ToolSpec

_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


@dataclass(frozen=True, slots=True)
class ToolReadiness:
    ready: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ToolContext:
    run_id: str
    agent_id: str
    tool_call_id: str
    operation_id: str | None = None

    @property
    def idempotency_key(self) -> str:
        return self.operation_id or f"{self.run_id}:{self.tool_call_id}"


@runtime_checkable
class Tool(Protocol):
    @property
    def spec(self) -> ToolSpec: ...

    @property
    def permissions(self) -> frozenset[str]: ...

    @property
    def timeout_seconds(self) -> float: ...

    @property
    def side_effect(self) -> SideEffect: ...

    @property
    def confirmation(self) -> Confirmation: ...

    def validate_arguments(self, arguments: Mapping[str, Any]) -> Mapping[str, Any]: ...

    async def check_readiness(self) -> ToolReadiness: ...

    async def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> Any: ...


type ReadinessCheck = Callable[[], bool | ToolReadiness | Awaitable[bool | ToolReadiness]]


class FunctionTool:
    """A normal typed Python function compiled into a governed Tool."""

    def __init__(
        self,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        permissions: frozenset[str] = frozenset(),
        timeout_seconds: float = 30.0,
        side_effect: SideEffect = SideEffect.READ_ONLY,
        confirmation: Confirmation = Confirmation.NONE,
        readiness: ReadinessCheck | None = None,
    ) -> None:
        tool_name = name or function.__name__
        if not tool_name or not _TOOL_NAME_PATTERN.fullmatch(tool_name):
            raise ValueError("tool name must contain only letters, numbers, '.', '_' or '-'")
        if timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be greater than zero")
        if confirmation is Confirmation.REQUIRED and side_effect is SideEffect.READ_ONLY:
            raise ValueError("read-only tools must not require confirmation")
        self._function = function
        self._arguments_model, self._context_parameter = _arguments_model(function)
        self._spec = ToolSpec(
            name=tool_name,
            description=description or inspect.getdoc(function) or f"Execute {tool_name}.",
            input_schema=self._arguments_model.model_json_schema(),
        )
        self._permissions = permissions
        self._timeout_seconds = timeout_seconds
        self._side_effect = side_effect
        self._confirmation = confirmation
        self._readiness = readiness

    @property
    def spec(self) -> ToolSpec:
        return self._spec

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
        return self._arguments_model.model_validate(dict(arguments)).model_dump()

    async def check_readiness(self) -> ToolReadiness:
        if self._readiness is None:
            return ToolReadiness(ready=True)
        result = self._readiness()
        if inspect.isawaitable(result):
            result = await result
        return result if isinstance(result, ToolReadiness) else ToolReadiness(bool(result))

    async def invoke(self, arguments: Mapping[str, Any], context: ToolContext) -> Any:
        keywords = dict(self.validate_arguments(arguments))
        if self._context_parameter is not None:
            keywords[self._context_parameter] = context
        if inspect.iscoroutinefunction(self._function):
            return await self._function(**keywords)
        return await asyncio.to_thread(self._function, **keywords)


@dataclass(frozen=True, slots=True)
class Toolkit:
    name: str
    tools: tuple[Tool, ...]

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("toolkit name must not be blank")
        names = [item.spec.name for item in self.tools]
        if len(set(names)) != len(names):
            raise ValueError(f"toolkit '{self.name}' contains duplicate tools")

    @classmethod
    def of(cls, name: str, *tools: Tool) -> Toolkit:
        return cls(name=name, tools=tools)


def flatten_tools(items: Iterable[Tool | Toolkit]) -> tuple[Tool, ...]:
    tools = tuple(
        tool for item in items for tool in (item.tools if isinstance(item, Toolkit) else (item,))
    )
    names = [item.spec.name for item in tools]
    if len(set(names)) != len(names):
        raise ValueError("registered tools must have unique names")
    return tools


def _arguments_model(function: Callable[..., Any]) -> tuple[type[BaseModel], str | None]:
    fields: dict[str, tuple[Any, Any]] = {}
    context_parameter: str | None = None
    annotations = get_type_hints(function)
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise TypeError("tool functions cannot declare *args or **kwargs")
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise TypeError("tool functions cannot declare positional-only parameters")
        annotation = annotations.get(parameter.name, parameter.annotation)
        if annotation is ToolContext:
            if context_parameter is not None or parameter.default is not inspect.Parameter.empty:
                raise TypeError("ToolContext must be one required parameter")
            context_parameter = parameter.name
            continue
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"tool parameter '{parameter.name}' requires a type annotation")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (annotation, default)
    model_name = f"{function.__name__.title().replace('_', '')}Arguments"
    return (
        cast(
            type[BaseModel],
            create_model(  # type: ignore[call-overload]
                model_name,
                __config__=ConfigDict(extra="forbid"),
                **fields,
            ),
        ),
        context_parameter,
    )


@overload
def tool(function: Callable[..., Any], /) -> FunctionTool: ...


@overload
def tool(
    function: None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: frozenset[str] = frozenset(),
    timeout_seconds: float = 30.0,
    side_effect: SideEffect = SideEffect.READ_ONLY,
    confirmation: Confirmation = Confirmation.NONE,
    readiness: ReadinessCheck | None = None,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def tool(
    function: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: frozenset[str] = frozenset(),
    timeout_seconds: float = 30.0,
    side_effect: SideEffect = SideEffect.READ_ONLY,
    confirmation: Confirmation = Confirmation.NONE,
    readiness: ReadinessCheck | None = None,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    def wrap(target: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            target,
            name=name,
            description=description,
            permissions=permissions,
            timeout_seconds=timeout_seconds,
            side_effect=side_effect,
            confirmation=confirmation,
            readiness=readiness,
        )

    return wrap(function) if function is not None else wrap


__all__ = [
    "AllowAllConfirmations",
    "AwaitHumanConfirmations",
    "Confirmation",
    "ConfirmationPolicy",
    "ConfirmationPolicyDecision",
    "DenyConfirmations",
    "FunctionTool",
    "SideEffect",
    "Tool",
    "ToolConfirmation",
    "ToolConfirmationDecision",
    "ToolContext",
    "Toolkit",
    "ToolReadiness",
    "ToolSpec",
    "flatten_tools",
    "tool",
]
