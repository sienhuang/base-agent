"""Task-local acquisition and release of application-defined resources."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar, cast

from base_agent.models import EventType
from base_agent.resources.models import ResourceFailure, ResourcePhase
from base_agent.stores import EventStore

if TYPE_CHECKING:
    from base_agent.runtime.context import RuntimeContext

T = TypeVar("T")
ResourceFactory = Callable[["RuntimeContext"], AbstractAsyncContextManager[Any]]
_RESOURCE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


class ResourceError(RuntimeError):
    """Base class for resource lifecycle errors."""


class ResourceNotFoundError(ResourceError):
    """The requested resource was not registered for this Agent."""


class ResourceScopeClosedError(ResourceError):
    """A resource was requested outside its active execution segment."""


class ResourceAcquisitionError(ResourceError):
    """A resource factory failed while entering its context manager."""


class ResourceTypeError(ResourceError):
    """A named resource did not have the type expected by its consumer."""


class ResourceTaskMismatchError(ResourceError):
    """A resource scope was accessed from a different asyncio task."""


@dataclass(frozen=True, slots=True)
class ResourceSpec:
    """Configuration for one lazily or eagerly acquired named resource."""

    name: str
    factory: ResourceFactory
    eager: bool = False

    def __post_init__(self) -> None:
        if not _RESOURCE_NAME.fullmatch(self.name):
            raise ValueError(f"invalid resource name '{self.name}'")


@dataclass(slots=True)
class _AcquiredResource:
    spec: ResourceSpec
    manager: AbstractAsyncContextManager[Any]
    value: Any


class _ScopeState(StrEnum):
    CREATED = "created"
    ACTIVE = "active"
    CLOSED = "closed"


class ResourceManager:
    """Acquire resources once and release them in reverse order in the same task."""

    def __init__(
        self,
        specs: tuple[ResourceSpec, ...],
        *,
        context: RuntimeContext,
        event_store: EventStore,
    ) -> None:
        names = [spec.name for spec in specs]
        if len(set(names)) != len(names):
            raise ValueError("resource names must be unique")
        self._specs = {spec.name: spec for spec in specs}
        self._context = context
        self._event_store = event_store
        self._acquired: dict[str, _AcquiredResource] = {}
        self._order: list[str] = []
        self._failures: list[ResourceFailure] = []
        self._state = _ScopeState.CREATED
        self._owner_task: asyncio.Task[Any] | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)

    @property
    def acquired_names(self) -> tuple[str, ...]:
        return tuple(self._order)

    async def start(self) -> None:
        if self._state is not _ScopeState.CREATED:
            raise ResourceScopeClosedError("resource scope can only be started once")
        self._owner_task = asyncio.current_task()
        self._state = _ScopeState.ACTIVE
        for spec in self._specs.values():
            if spec.eager:
                await self.get(spec.name)

    async def get(self, name: str, expected_type: type[T] | None = None) -> T:
        if self._state is not _ScopeState.ACTIVE:
            raise ResourceScopeClosedError("resources are only available during execution")
        if asyncio.current_task() is not self._owner_task:
            raise ResourceTaskMismatchError(
                "resources must be acquired and released in their owning asyncio task"
            )
        acquired = self._acquired.get(name)
        if acquired is None:
            try:
                spec = self._specs[name]
            except KeyError as exc:
                raise ResourceNotFoundError(f"unknown resource '{name}'") from exc
            acquired = await self._acquire(spec)
        value = acquired.value
        if expected_type is not None and not isinstance(value, expected_type):
            raise ResourceTypeError(
                f"resource '{name}' expected {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        return cast(T, value)

    async def _acquire(self, spec: ResourceSpec) -> _AcquiredResource:
        manager = spec.factory(self._context)
        try:
            value = await manager.__aenter__()
        except Exception as exc:
            failure = ResourceFailure(
                name=spec.name,
                phase=ResourcePhase.ACQUIRE,
                message=str(exc),
            )
            self._failures.append(failure)
            await self._emit_failure(failure)
            raise ResourceAcquisitionError(
                f"failed to acquire resource '{spec.name}': {exc}"
            ) from exc
        acquired = _AcquiredResource(spec=spec, manager=manager, value=value)
        self._acquired[spec.name] = acquired
        self._order.append(spec.name)
        await self._event_store.emit(
            self._context.run_id,
            EventType.RESOURCE_ACQUIRED,
            {"name": spec.name},
        )
        return acquired

    async def close(self, error: BaseException | None = None) -> tuple[ResourceFailure, ...]:
        if self._state is _ScopeState.CLOSED:
            return tuple(self._failures)
        self._state = _ScopeState.CLOSED
        failures: list[ResourceFailure] = []
        exc_type: type[BaseException] | None = type(error) if error is not None else None
        traceback: TracebackType | None = error.__traceback__ if error is not None else None
        for name in reversed(self._order):
            acquired = self._acquired[name]
            try:
                await acquired.manager.__aexit__(exc_type, error, traceback)
            except Exception as exc:
                failure = ResourceFailure(
                    name=name,
                    phase=ResourcePhase.RELEASE,
                    message=str(exc),
                )
                failures.append(failure)
                self._failures.append(failure)
                await self._emit_failure(failure)
            else:
                await self._event_store.emit(
                    self._context.run_id,
                    EventType.RESOURCE_RELEASED,
                    {"name": name},
                )
        self._acquired.clear()
        self._order.clear()
        return tuple(self._failures)

    async def _emit_failure(self, failure: ResourceFailure) -> None:
        await self._event_store.emit(
            self._context.run_id,
            EventType.RESOURCE_FAILED,
            failure.model_dump(mode="json"),
        )
