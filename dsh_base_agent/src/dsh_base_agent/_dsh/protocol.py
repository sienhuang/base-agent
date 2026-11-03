"""Narrow adapter around the official synchronous DSH Python SDK."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

type JsonObject = dict[str, Any]
type DshEventHandler = Callable[[JsonObject], Awaitable[None]]
type DshInput = str | list[JsonObject]


@dataclass(frozen=True, slots=True)
class RuntimeSpec:
    agent_name: str
    fingerprint: str
    instructions: str
    tool_names: tuple[str, ...]
    tool_gateway_url: str | None
    context_configuration: dict[str, object] | None
    provider: str
    model: str
    allow_native_tools: bool = False
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class DshRunResult:
    session_id: str
    final_response: str
    finish_reason: str | None
    events: tuple[JsonObject, ...] = ()


@runtime_checkable
class DshRuntime(Protocol):
    async def start(self) -> None: ...

    async def run(
        self,
        input: DshInput,
        *,
        session_id: str,
        on_event: DshEventHandler | None = None,
    ) -> DshRunResult: ...

    async def cancel(self, session_id: str) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class DshRuntimeFactory(Protocol):
    def create(self, spec: RuntimeSpec, *, dsh_home: Path) -> DshRuntime: ...


__all__ = [
    "DshEventHandler",
    "DshInput",
    "DshRunResult",
    "DshRuntime",
    "DshRuntimeFactory",
    "JsonObject",
    "RuntimeSpec",
]
