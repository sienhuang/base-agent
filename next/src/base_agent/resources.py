"""Run-scoped Resource acquisition and deterministic cleanup."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar, cast

from base_agent.events import EventEmitter

T = TypeVar("T")
ResourceFactory = Callable[
    ["ResourceContext"],
    Any | Awaitable[Any] | AbstractAsyncContextManager[Any],
]


class ResourceScope(StrEnum):
    RUN = "run"


@dataclass(frozen=True, slots=True)
class ResourceContext:
    run_id: str
    agent_id: str
    flow_run_id: str | None = None
    invocation_id: str | None = None
    stage: str | None = None
    step_id: str | None = None
    attempt: int | None = None


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    name: str
    factory: ResourceFactory
    scope: ResourceScope = ResourceScope.RUN
    eager: bool = False

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("resource name must not be blank")
        if self.name != self.name.strip():
            raise ValueError("resource name must not have surrounding whitespace")


class ResourceNotFoundError(LookupError):
    pass


class ResourceManager:
    """Lazily acquires one instance per spec and releases it in reverse order."""

    def __init__(
        self,
        specs: tuple[ResourceSpec, ...],
        *,
        context: ResourceContext,
        emitter: EventEmitter | None = None,
    ) -> None:
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError("registered resources must have unique names")
        self._specs = {spec.name: spec for spec in specs}
        self._context = context
        self._emitter = emitter
        self._stack = AsyncExitStack()
        self._values: dict[str, Any] = {}
        self._closed = False

    async def start(self) -> None:
        for spec in self._specs.values():
            if spec.eager:
                await self.get(spec.name)

    async def get(self, name: str, expected_type: type[T] | None = None) -> T:
        if self._closed:
            raise RuntimeError("ResourceManager is closed")
        if name in self._values:
            value = self._values[name]
        else:
            try:
                spec = self._specs[name]
            except KeyError as exc:
                raise ResourceNotFoundError(f"resource '{name}' is not registered") from exc
            if self._emitter is not None:
                await self._emitter.emit("resource.acquiring", resource=name)
            try:
                produced = spec.factory(self._context)
                if inspect.isawaitable(produced):
                    produced = await produced
                if hasattr(produced, "__aenter__") and hasattr(produced, "__aexit__"):
                    value = await self._stack.enter_async_context(
                        cast(AbstractAsyncContextManager[Any], produced)
                    )
                else:
                    value = produced
            except Exception as exc:
                if self._emitter is not None:
                    await self._emitter.emit(
                        "resource.failed",
                        resource=name,
                        error_type=type(exc).__name__,
                    )
                raise
            self._values[name] = value
            if self._emitter is not None:
                await self._emitter.emit("resource.acquired", resource=name)
        if expected_type is not None and not isinstance(value, expected_type):
            raise TypeError(
                f"resource '{name}' is {type(value).__name__}, "
                f"expected {expected_type.__name__}"
            )
        return cast(T, value)

    async def close(self) -> None:
        if self._closed:
            return
        try:
            await self._stack.aclose()
        finally:
            self._closed = True

    async def __aenter__(self) -> ResourceManager:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()
