"""Small composition object for Agent/Flow HTTP applications."""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager

from fastapi import FastAPI

from base_agent.agent import Agent
from base_agent.server.app import create_http_app
from base_agent.server.registry import RegisteredFlow

Preflight = Callable[[], None | Awaitable[None]]


class AgentApplication:
    """Compose optional Agent Runs, named Flows, preflight, and FastAPI."""

    def __init__(
        self,
        *,
        agent: Agent | None = None,
        flows: Mapping[str, RegisteredFlow] | None = None,
        preflight: Sequence[Preflight] = (),
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
        self.title = title
        self.prefix = prefix
        self.sse_ping_seconds = sse_ping_seconds

    def fastapi(self) -> FastAPI:
        """Create the process-local HTTP adapter for this composition."""

        @asynccontextmanager
        async def lifespan(_: FastAPI) -> AsyncIterator[None]:
            for check in self.preflight:
                result = check()
                if inspect.isawaitable(result):
                    await result
            yield

        return create_http_app(
            self.agent,
            prefix=self.prefix,
            title=self.title,
            sse_ping_seconds=self.sse_ping_seconds,
            flows=self.flows,
            lifespan=lifespan,
        )
