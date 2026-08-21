"""Small composition object for Agent/Flow HTTP applications."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from base_agent.agent import Agent
from base_agent.events import EventStore
from base_agent.execution import ExecutionStore
from base_agent.run_store import RunStore
from base_agent.server.app import OperatorAuthorizer, create_http_app
from base_agent.server.registry import RegisteredFlow

LifecycleCallback = Callable[[], None | Awaitable[None]]
Preflight = LifecycleCallback
Shutdown = LifecycleCallback


class AgentApplication:
    """Compose optional Agent Runs, named Flows, preflight, and FastAPI."""

    def __init__(
        self,
        *,
        agent: Agent | None = None,
        flows: Mapping[str, RegisteredFlow] | None = None,
        preflight: Sequence[Preflight] = (),
        shutdown: Sequence[Shutdown] = (),
        event_store: EventStore | None = None,
        run_store: RunStore | None = None,
        run_lease_seconds: float = 30.0,
        run_heartbeat_interval_seconds: float | None = None,
        runtime_build_id: str | None = None,
        execution_store: ExecutionStore | None = None,
        execution_mode: str = "local",
        runtime_route: str = "default",
        operator_authorizer: OperatorAuthorizer | None = None,
        title: str = "base-agent Application",
        prefix: str = "/v1",
        sse_ping_seconds: int = 15,
    ) -> None:
        resolved_flows = dict(flows or {})
        if agent is None and not resolved_flows:
            raise ValueError("AgentApplication requires at least one Agent or Flow")
        self.agent = agent
        self.flows = resolved_flows
        self.preflight = tuple(preflight)
        self.shutdown = tuple(shutdown)
        self.event_store = event_store
        self.run_store = run_store
        self.run_lease_seconds = run_lease_seconds
        self.run_heartbeat_interval_seconds = run_heartbeat_interval_seconds
        self.runtime_build_id = runtime_build_id
        self.execution_store = execution_store
        self.execution_mode = execution_mode
        self.runtime_route = runtime_route
        self.operator_authorizer = operator_authorizer
        self.title = title
        self.prefix = prefix
        self.sse_ping_seconds = sse_ping_seconds

    def fastapi(self) -> FastAPI:
        """Create local or queued execution routes with persistent read recovery."""

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            try:
                for check in self.preflight:
                    result = check()
                    if inspect.isawaitable(result):
                        await result
                yield
            finally:
                for cleanup in reversed(self.shutdown):
                    result = cleanup()
                    if inspect.isawaitable(result):
                        await result

        return create_http_app(
            self.agent,
            prefix=self.prefix,
            title=self.title,
            sse_ping_seconds=self.sse_ping_seconds,
            flows=self.flows,
            lifespan=lifespan,
            event_store=self.event_store,
            run_store=self.run_store,
            run_lease_seconds=self.run_lease_seconds,
            run_heartbeat_interval_seconds=self.run_heartbeat_interval_seconds,
            runtime_build_id=self.runtime_build_id,
            execution_store=self.execution_store,
            execution_mode=self.execution_mode,
            runtime_route=self.runtime_route,
            operator_authorizer=self.operator_authorizer,
        )
