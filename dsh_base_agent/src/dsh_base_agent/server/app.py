"""Standalone FastAPI control plane for DSH-backed Agents and Flows."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, Protocol

from fastapi import FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sse_starlette.sse import EventSourceResponse

from dsh_base_agent.agent import Agent
from dsh_base_agent.app import BaseAgentApp
from dsh_base_agent.models import Event, RunResult, RunStatus
from dsh_base_agent.orchestration import (
    FlowResult,
    FlowRunHandle,
    FlowStatus,
    PlannerReActResult,
)
from dsh_base_agent.run_handle import RunHandle, RunNotCancellableError


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)


class StartRunResponse(BaseModel):
    run_id: str
    status: RunStatus
    agent_id: str


class RunResponse(BaseModel):
    run_id: str
    status: RunStatus
    output: Any = None
    text: str = ""
    error: str | None = None


class EventResponse(BaseModel):
    sequence: int
    kind: str
    run_id: str
    payload: dict[str, Any]
    turn: int | None = None


class StartFlowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    flow_run_id: str | None = Field(default=None, min_length=1)


class StartFlowResponse(BaseModel):
    flow_run_id: str
    status: FlowStatus


class FlowResponse(BaseModel):
    flow_run_id: str
    status: FlowStatus
    output: str = ""
    error: str | None = None


class RegisteredFlow(Protocol):
    def start(self, prompt: str, *, flow_run_id: str | None = None) -> FlowRunHandle[Any]: ...


class _RunRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, RunHandle] = {}

    def add(self, handle: RunHandle) -> None:
        if handle.run_id in self._handles:
            raise ValueError(f"Run '{handle.run_id}' already exists")
        self._handles[handle.run_id] = handle

    def get(self, run_id: str) -> RunHandle:
        try:
            return self._handles[run_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Run not found") from exc


class _FlowRegistry:
    def __init__(self) -> None:
        self._handles: dict[str, FlowRunHandle[Any]] = {}

    def add(self, handle: FlowRunHandle[Any]) -> None:
        if handle.flow_run_id in self._handles:
            raise ValueError(f"Flow Run '{handle.flow_run_id}' already exists")
        self._handles[handle.flow_run_id] = handle

    def get(self, flow_run_id: str) -> FlowRunHandle[Any]:
        try:
            return self._handles[flow_run_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Flow Run not found") from exc


def create_http_app(
    base_app: BaseAgentApp,
    agent: Agent,
    *,
    agents: Mapping[str, Agent] | None = None,
    flows: Mapping[str, RegisteredFlow] | None = None,
    prepare_agents: Sequence[Agent] = (),
    title: str = "DSH Base Agent",
    prefix: str = "/v1",
    sse_ping_seconds: int = 15,
) -> FastAPI:
    """Create a complete control plane without importing another Agent framework."""

    hosted_agents = dict(agents or {agent.name: agent})
    hosted_agents.setdefault(agent.name, agent)
    for current in prepare_agents:
        hosted_agents.setdefault(current.name, current)
    hosted_flows = dict(flows or {})
    runs, flow_runs = _RunRegistry(), _FlowRegistry()

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.dsh_ready = False
        try:
            for current in hosted_agents.values():
                await current.prepare()
            application.state.dsh_ready = True
            yield
        finally:
            application.state.dsh_ready = False
            await base_app.close()

    application = FastAPI(title=title, lifespan=lifespan)
    application.state.dsh_app = base_app
    application.state.agent = agent
    application.state.agents = hosted_agents
    application.state.flows = hosted_flows
    application.state.runs = runs
    application.state.flow_runs = flow_runs
    application.state.dsh_ready = False

    @application.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    async def ready() -> dict[str, str]:
        if not application.state.dsh_ready:
            raise HTTPException(status_code=503, detail="DSH Runtime is not ready")
        return {"status": "ok", "dsh": "ready"}

    @application.post(
        f"{prefix}/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(request: StartRunRequest) -> StartRunResponse:
        selected_id = request.agent_id or agent.name
        try:
            selected = hosted_agents[selected_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent not found") from exc
        try:
            handle = selected.start(request.prompt, run_id=request.run_id)
            runs.add(handle)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StartRunResponse(
            run_id=handle.run_id,
            status=RunStatus.RUNNING,
            agent_id=selected_id,
        )

    @application.get(f"{prefix}/runs/{{run_id}}", response_model=RunResponse)
    async def get_run(run_id: str) -> RunResponse:
        handle = runs.get(run_id)
        if handle.status is RunStatus.RUNNING:
            return RunResponse(run_id=run_id, status=RunStatus.RUNNING)
        return _run_response(await handle.wait())

    @application.post(f"{prefix}/runs/{{run_id}}/cancel", response_model=RunResponse)
    async def cancel_run(run_id: str) -> RunResponse:
        try:
            return _run_response(await runs.get(run_id).cancel())
        except RunNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get(
        f"{prefix}/runs/{{run_id}}/events",
        response_model=list[EventResponse],
    )
    async def run_events(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> list[EventResponse]:
        return [
            _event_response(item)
            for item in await runs.get(run_id).events(after_sequence=after_sequence)
        ]

    @application.get(f"{prefix}/runs/{{run_id}}/events/stream")
    async def stream_run_events(
        run_id: str,
        after_sequence: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> EventSourceResponse:
        cursor = _cursor(after_sequence, last_event_id)
        handle = runs.get(run_id)
        return EventSourceResponse(
            _sse(handle.stream(after_sequence=cursor)),
            ping=sse_ping_seconds,
        )

    @application.post(
        f"{prefix}/flows/{{flow_id}}",
        response_model=StartFlowResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_flow(flow_id: str, request: StartFlowRequest) -> StartFlowResponse:
        try:
            flow = hosted_flows[flow_id]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Flow not found") from exc
        try:
            handle = flow.start(request.prompt, flow_run_id=request.flow_run_id)
            flow_runs.add(handle)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StartFlowResponse(
            flow_run_id=handle.flow_run_id,
            status=FlowStatus.RUNNING,
        )

    @application.get(f"{prefix}/flows/{{flow_run_id}}", response_model=FlowResponse)
    async def get_flow(flow_run_id: str) -> FlowResponse:
        handle = flow_runs.get(flow_run_id)
        if handle.status is FlowStatus.RUNNING:
            return FlowResponse(flow_run_id=flow_run_id, status=FlowStatus.RUNNING)
        return _flow_response(await handle.wait())

    @application.post(
        f"{prefix}/flows/{{flow_run_id}}/cancel",
        response_model=FlowResponse,
    )
    async def cancel_flow(flow_run_id: str) -> FlowResponse:
        try:
            return _flow_response(await flow_runs.get(flow_run_id).cancel())
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @application.get(
        f"{prefix}/flows/{{flow_run_id}}/events",
        response_model=list[EventResponse],
    )
    async def flow_events(
        flow_run_id: str,
        after_sequence: int = Query(default=0, ge=0),
    ) -> list[EventResponse]:
        return [
            _event_response(item)
            for item in await flow_runs.get(flow_run_id).events(after_sequence=after_sequence)
        ]

    @application.get(f"{prefix}/flows/{{flow_run_id}}/events/stream")
    async def stream_flow_events(
        flow_run_id: str,
        after_sequence: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> EventSourceResponse:
        cursor = _cursor(after_sequence, last_event_id)
        handle = flow_runs.get(flow_run_id)
        return EventSourceResponse(
            _sse(handle.stream(after_sequence=cursor)),
            ping=sse_ping_seconds,
        )

    return application


def _event_response(event: Event) -> EventResponse:
    return EventResponse(
        sequence=event.sequence,
        kind=event.kind,
        run_id=event.run_id,
        payload=dict(event.payload),
        turn=event.turn,
    )


def _run_response(result: RunResult) -> RunResponse:
    output = result.output
    if isinstance(output, BaseModel):
        output = output.model_dump(mode="json")
    return RunResponse(
        run_id=result.run_id,
        status=result.status,
        output=output,
        text=result.text,
        error=result.error,
    )


def _flow_response(result: FlowResult | PlannerReActResult) -> FlowResponse:
    return FlowResponse(
        flow_run_id=result.flow_run_id,
        status=result.status,
        output=result.output,
        error=result.error,
    )


def _cursor(query: int, header: str | None) -> int:
    if header is None:
        return query
    try:
        parsed = int(header)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
    return max(query, parsed)


async def _sse(events: AsyncIterator[Event]) -> AsyncIterator[dict[str, str]]:
    async for event in events:
        yield {
            "id": str(event.sequence),
            "event": event.kind,
            "data": json.dumps(
                _event_response(event).model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }


__all__ = [
    "EventResponse",
    "FlowResponse",
    "RegisteredFlow",
    "RunResponse",
    "StartFlowRequest",
    "StartFlowResponse",
    "StartRunRequest",
    "StartRunResponse",
    "create_http_app",
]
