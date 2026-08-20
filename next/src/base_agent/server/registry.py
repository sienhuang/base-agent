"""Process-local retention for background Run handles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from base_agent.agent import Agent
from base_agent.flows import FlowRunHandle
from base_agent.governance import ToolConfirmation
from base_agent.models import RunResult
from base_agent.run_handle import RunHandle


class RunNotFoundError(LookupError):
    """The requested process-local Run is unknown."""


class FlowNotRegisteredError(LookupError):
    """The requested Flow strategy is not registered by the application."""


class FlowRunNotFoundError(LookupError):
    """The requested process-local Flow Run is unknown."""


class RegisteredFlow(Protocol):
    """A Flow strategy that can launch a process-local background execution."""

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
    ) -> FlowRunHandle[Any]: ...



@runtime_checkable
class ResumableRegisteredFlow(Protocol):
    async def resume(self, flow_run_id: str, user_input: str) -> Any: ...

    async def confirm(self, flow_run_id: str, confirmation: ToolConfirmation) -> Any: ...


class InMemoryRunRegistry:
    """Retain RunHandles for HTTP status polling in one server process."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._handles: dict[str, RunHandle] = {}
        self._continued_results: dict[str, RunResult] = {}

    def start(self, prompt: str, *, stream: bool = False) -> RunHandle:
        handle = self._agent.start(prompt, stream=stream)
        self._handles[handle.run_id] = handle
        return handle

    def get(self, run_id: str) -> RunHandle:
        try:
            return self._handles[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"Run '{run_id}' was not found") from exc

    def continued_result(self, run_id: str) -> RunResult | None:
        return self._continued_results.get(run_id)

    async def resume(self, run_id: str, user_input: str) -> RunResult:
        result = await self._agent.resume(run_id, user_input)
        self._continued_results[run_id] = result
        return result

    async def confirm(self, run_id: str, confirmation: ToolConfirmation) -> RunResult:
        result = await self._agent.confirm(run_id, confirmation)
        self._continued_results[run_id] = result
        return result

    async def cancel_waiting(self, run_id: str) -> RunResult:
        result = await self._agent.cancel(run_id)
        self._continued_results[run_id] = result
        return result


class InMemoryFlowRegistry:
    """Retain named Flow factories and their process-local background handles."""

    def __init__(self, flows: Mapping[str, RegisteredFlow]) -> None:
        if any(not name.strip() for name in flows):
            raise ValueError("Flow registration names must not be blank")
        self._flows = dict(flows)
        self._handles: dict[str, FlowRunHandle[Any]] = {}
        self._owners: dict[str, RegisteredFlow] = {}
        self._continued_results: dict[str, Any] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._flows)

    def start(
        self,
        name: str,
        prompt: str,
        *,
        stream: bool = False,
    ) -> FlowRunHandle[Any]:
        try:
            flow = self._flows[name]
        except KeyError as exc:
            raise FlowNotRegisteredError(f"Flow '{name}' is not registered") from exc
        handle = flow.start(prompt, stream=stream)
        self._handles[handle.flow_run_id] = handle
        self._owners[handle.flow_run_id] = flow
        return handle

    def get(self, flow_run_id: str) -> FlowRunHandle[Any]:
        try:
            return self._handles[flow_run_id]
        except KeyError as exc:
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' was not found"
            ) from exc

    def continued_result(self, flow_run_id: str) -> Any | None:
        return self._continued_results.get(flow_run_id)

    async def resume(self, flow_run_id: str, user_input: str) -> Any:
        flow = self._owner(flow_run_id)
        if not isinstance(flow, ResumableRegisteredFlow):
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' does not support WAITING continuation"
            )
        result = await flow.resume(flow_run_id, user_input)
        self._continued_results[flow_run_id] = result
        return result

    async def confirm(self, flow_run_id: str, confirmation: ToolConfirmation) -> Any:
        flow = self._owner(flow_run_id)
        if not isinstance(flow, ResumableRegisteredFlow):
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' does not support WAITING continuation"
            )
        result = await flow.confirm(flow_run_id, confirmation)
        self._continued_results[flow_run_id] = result
        return result

    def _owner(self, flow_run_id: str) -> RegisteredFlow:
        try:
            return self._owners[flow_run_id]
        except KeyError as exc:
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' is not owned by this server process"
            ) from exc
