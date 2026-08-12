"""FastAPI routes over the public Agent and RunHandle APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Query, status
from sse_starlette import EventSourceResponse, ServerSentEvent

from base_agent.agent import Agent
from base_agent.models import Event, RunStatus
from base_agent.run_handle import EventStreamingNotSupportedError, RunHandle
from base_agent.server.registry import InMemoryRunRegistry, RunNotFoundError
from base_agent.server.schemas import (
    EventResponse,
    RunResponse,
    StartRunRequest,
    StartRunResponse,
)


def create_http_app(
    agent: Agent,
    *,
    prefix: str = "/v1",
    title: str = "base-agent Run Server",
    sse_ping_seconds: int = 15,
) -> FastAPI:
    """Create an HTTP adapter for process-local background Runs."""
    if not prefix.startswith("/") or (len(prefix) > 1 and prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and must not end with '/'")
    if sse_ping_seconds < 1:
        raise ValueError("sse_ping_seconds must be at least 1")
    route_prefix = "" if prefix == "/" else prefix
    registry = InMemoryRunRegistry(agent)
    app = FastAPI(title=title)
    app.state.run_registry = registry
    app.state.sse_ping_seconds = sse_ping_seconds

    @app.post(
        f"{route_prefix}/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(payload: StartRunRequest) -> StartRunResponse:
        handle = registry.start(payload.prompt, stream=payload.stream)
        return StartRunResponse(run_id=handle.run_id, status=handle.status)

    @app.get(
        f"{route_prefix}/runs/{{run_id}}",
        response_model=RunResponse,
    )
    async def get_run(run_id: str) -> RunResponse:
        handle = _get_handle(registry, run_id)
        return await _run_response(handle)

    @app.get(
        f"{route_prefix}/runs/{{run_id}}/events",
        response_model=tuple[EventResponse, ...],
    )
    async def list_events(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[EventResponse, ...]:
        """Replay the persisted Event history strictly after one cursor."""
        handle = _get_handle(registry, run_id)
        return tuple(
            _event_response(event)
            for event in await handle.events(after_sequence=after_sequence)
        )

    @app.get(f"{route_prefix}/runs/{{run_id}}/events/stream")
    async def stream_events(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        last_event_id_header: Annotated[
            str | None,
            Header(alias="Last-Event-ID"),
        ] = None,
        last_event_id_query: Annotated[
            str | None,
            Query(alias="last_event_id"),
        ] = None,
    ) -> EventSourceResponse:
        """Replay and follow Events over SSE until the Run reaches a terminal state."""
        handle = _get_handle(registry, run_id)
        cursor = _event_cursor(
            after_sequence,
            last_event_id_header or last_event_id_query,
        )
        try:
            events = handle.stream(after_sequence=cursor)
        except EventStreamingNotSupportedError as exc:
            raise HTTPException(status_code=501, detail=str(exc)) from exc
        return EventSourceResponse(
            _sse_events(events),
            ping=sse_ping_seconds,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _get_handle(registry: InMemoryRunRegistry, run_id: str) -> RunHandle:
    try:
        return registry.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _event_cursor(after_sequence: int, last_event_id: str | None) -> int:
    if last_event_id is None:
        return after_sequence
    try:
        header_cursor = int(last_event_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be an integer") from exc
    if header_cursor < 0:
        raise HTTPException(status_code=400, detail="Last-Event-ID must be non-negative")
    return max(after_sequence, header_cursor)


def _event_response(event: Event) -> EventResponse:
    return EventResponse(
        sequence=event.sequence,
        kind=event.kind,
        run_id=event.run_id,
        payload=dict(event.payload),
    )


async def _sse_events(events: AsyncIterator[Event]) -> AsyncIterator[ServerSentEvent]:
    async for event in events:
        yield ServerSentEvent(
            id=str(event.sequence),
            event=event.kind,
            data=_event_response(event).model_dump_json(),
        )


async def _run_response(handle: RunHandle) -> RunResponse:
    current_status = handle.status
    if current_status is RunStatus.RUNNING:
        return RunResponse(
            run_id=handle.run_id,
            status=current_status,
            event_count=len(await handle.events()),
        )
    try:
        result = await handle.wait()
    except Exception as exc:
        return RunResponse(
            run_id=handle.run_id,
            status=RunStatus.FAILED,
            event_count=len(await handle.events()),
            error=f"{type(exc).__name__}: {exc}",
        )
    return RunResponse(
        run_id=handle.run_id,
        status=result.status,
        event_count=len(await handle.events()),
        output=result.output,
        text=result.text,
        error=result.error,
    )
