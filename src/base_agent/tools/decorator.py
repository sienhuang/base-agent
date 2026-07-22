"""Convert typed Python functions into runtime tools."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, get_type_hints, overload

from pydantic import BaseModel, ConfigDict, create_model

from base_agent.models import ToolDefinition
from base_agent.tools.context import ToolContext


class FunctionTool:
    """A Tool backed by one typed Python function."""

    def __init__(
        self,
        function: Callable[..., Any],
        *,
        name: str | None = None,
        description: str | None = None,
        permissions: frozenset[str] = frozenset(),
        timeout_seconds: float = 30.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("tool timeout_seconds must be greater than zero")

        self._function = function
        self._uses_context = _uses_tool_context(function)
        self._argument_model = _create_argument_model(function)
        tool_name = name or function.__name__
        tool_description = description or inspect.getdoc(function) or f"Execute {tool_name}."
        self._definition = ToolDefinition(
            name=tool_name,
            description=tool_description,
            input_schema=self._argument_model.model_json_schema(),
        )
        self._permissions = permissions
        self._timeout_seconds = timeout_seconds

    @property
    def definition(self) -> ToolDefinition:
        return self._definition

    @property
    def permissions(self) -> frozenset[str]:
        return self._permissions

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    async def invoke(self, arguments: Mapping[str, Any]) -> Any:
        return await self._invoke(arguments, context=None)

    async def invoke_with_context(
        self,
        arguments: Mapping[str, Any],
        context: ToolContext,
    ) -> Any:
        return await self._invoke(arguments, context=context)

    async def _invoke(
        self,
        arguments: Mapping[str, Any],
        *,
        context: ToolContext | None,
    ) -> Any:
        validated = self._argument_model.model_validate(dict(arguments))
        keyword_arguments = validated.model_dump()
        if self._uses_context:
            if context is None:
                raise RuntimeError("tool requires an execution context")
            keyword_arguments["context"] = context
        if inspect.iscoroutinefunction(self._function):
            awaitable = self._function(**keyword_arguments)
            return await _await_result(awaitable)
        return await asyncio.to_thread(self._function, **keyword_arguments)


def _create_argument_model(function: Callable[..., Any]) -> type[BaseModel]:
    fields: dict[str, tuple[Any, Any]] = {}
    annotations = get_type_hints(function)
    for parameter in inspect.signature(function).parameters.values():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise TypeError("tool functions cannot declare *args or **kwargs")
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise TypeError("tool functions cannot declare positional-only parameters")
        annotation = annotations.get(parameter.name, parameter.annotation)
        if annotation is ToolContext:
            if parameter.name != "context":
                raise TypeError("ToolContext parameter must be named 'context'")
            continue
        if annotation is inspect.Parameter.empty:
            raise TypeError(f"tool parameter '{parameter.name}' requires a type annotation")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (annotation, default)

    model_name = f"{function.__name__.title().replace('_', '')}Arguments"
    argument_model: type[BaseModel] = create_model(  # type: ignore[call-overload]
        model_name,
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    return argument_model


def _uses_tool_context(function: Callable[..., Any]) -> bool:
    annotations = get_type_hints(function)
    return any(
        annotations.get(parameter.name, parameter.annotation) is ToolContext
        for parameter in inspect.signature(function).parameters.values()
    )


async def _await_result(value: Awaitable[Any]) -> Any:
    return await value


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
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def tool(
    function: Callable[..., Any] | None = None,
    /,
    *,
    name: str | None = None,
    description: str | None = None,
    permissions: frozenset[str] = frozenset(),
    timeout_seconds: float = 30.0,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    """Decorate an async or sync typed function as a runtime Tool."""

    def wrap(target: Callable[..., Any]) -> FunctionTool:
        return FunctionTool(
            target,
            name=name,
            description=description,
            permissions=permissions,
            timeout_seconds=timeout_seconds,
        )

    return wrap(function) if function is not None else wrap
