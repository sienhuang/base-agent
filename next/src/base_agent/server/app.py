"""FastAPI routes over the public Agent and RunHandle APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, status
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.types import Lifespan

from base_agent.agent import Agent
from base_agent.flows import (
    FlowResult,
    FlowRunHandle,
    FlowRunNotCancellableError,
    FlowStatus,
    PlannerReActResult,
)
from base_agent.models import Event, RunStatus
from base_agent.run_handle import (
    EventStreamingNotSupportedError,
    RunHandle,
    RunNotCancellableError,
)
from base_agent.server.registry import (
    FlowNotRegisteredError,
    FlowRunNotFoundError,
    InMemoryFlowRegistry,
    InMemoryRunRegistry,
    RegisteredFlow,
    RunNotFoundError,
)
from base_agent.server.schemas import (
    ArtifactRefResponse,
    EventResponse,
    FlowChildRunResponse,
    FlowResponse,
    RunResponse,
    StartFlowRequest,
    StartFlowResponse,
    StartRunRequest,
    StartRunResponse,
)


def create_http_app(
    agent: Agent | None = None,
    *,
    prefix: str = "/v1",
    title: str = "base-agent Run Server",
    sse_ping_seconds: int = 15,
    flows: Mapping[str, RegisteredFlow] | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
) -> FastAPI:
    """Create an HTTP adapter for process-local Agent and named Flow Runs."""
    if not prefix.startswith("/") or (len(prefix) > 1 and prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and must not end with '/'")
    if sse_ping_seconds < 1:
        raise ValueError("sse_ping_seconds must be at least 1")
    route_prefix = "" if prefix == "/" else prefix
    if agent is None and not flows:
        raise ValueError("create_http_app requires at least one Agent or Flow")
    registry = InMemoryRunRegistry(agent) if agent is not None else None
    flow_registry = InMemoryFlowRegistry(flows or {})
    app = FastAPI(title=title, lifespan=lifespan)
    app.state.run_registry = registry
    app.state.flow_registry = flow_registry
    app.state.sse_ping_seconds = sse_ping_seconds

    if registry is not None:
        _register_run_routes(
            app,
            registry,
            route_prefix=route_prefix,
            sse_ping_seconds=sse_ping_seconds,
        )

    if flow_registry.names:
        for flow_name in flow_registry.names:
            app.add_api_route(
                f"{route_prefix}/flows/{flow_name}",
                _start_flow_endpoint(flow_registry, flow_name),
                methods=["POST"],
                response_model=StartFlowResponse,
                status_code=status.HTTP_202_ACCEPTED,
                name=f"start-flow-{flow_name}",
            )

        @app.get(
            f"{route_prefix}/flows/{{flow_run_id}}",
            response_model=FlowResponse,
        )
        async def get_flow(flow_run_id: str) -> FlowResponse:
            return await _flow_response(_get_flow_handle(flow_registry, flow_run_id))

        @app.post(
            f"{route_prefix}/flows/{{flow_run_id}}/cancel",
            response_model=FlowResponse,
        )
        async def cancel_flow(flow_run_id: str) -> FlowResponse:
            handle = _get_flow_handle(flow_registry, flow_run_id)
            try:
                await handle.cancel()
            except FlowRunNotCancellableError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return await _flow_response(handle)

        @app.get(
            f"{route_prefix}/flows/{{flow_run_id}}/events",
            response_model=tuple[EventResponse, ...],
        )
        async def list_flow_events(
            flow_run_id: str,
            after_sequence: Annotated[int, Query(ge=0)] = 0,
        ) -> tuple[EventResponse, ...]:
            handle = _get_flow_handle(flow_registry, flow_run_id)
            return tuple(
                _event_response(event)
                for event in await handle.events(after_sequence=after_sequence)
            )

        @app.get(f"{route_prefix}/flows/{{flow_run_id}}/events/stream")
        async def stream_flow_events(
            flow_run_id: str,
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
            handle = _get_flow_handle(flow_registry, flow_run_id)
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


def _register_run_routes(
    app: FastAPI,
    registry: InMemoryRunRegistry,
    *,
    route_prefix: str,
    sse_ping_seconds: int,
) -> None:
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

    @app.post(
        f"{route_prefix}/runs/{{run_id}}/cancel",
        response_model=RunResponse,
    )
    async def cancel_run(run_id: str) -> RunResponse:
        handle = _get_handle(registry, run_id)
        try:
            await handle.cancel()
        except RunNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
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


def _get_handle(registry: InMemoryRunRegistry, run_id: str) -> RunHandle:
    try:
        return registry.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _get_flow_handle(
    registry: InMemoryFlowRegistry,
    flow_run_id: str,
) -> FlowRunHandle[Any]:
    try:
        return registry.get(flow_run_id)
    except FlowRunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _start_flow_endpoint(
    registry: InMemoryFlowRegistry,
    flow_name: str,
) -> Callable[[StartFlowRequest], Awaitable[StartFlowResponse]]:
    async def start_flow(payload: StartFlowRequest) -> StartFlowResponse:
        try:
            handle = registry.start(
                flow_name,
                payload.prompt,
                stream=payload.stream,
            )
        except FlowNotRegisteredError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return StartFlowResponse(
            flow_run_id=handle.flow_run_id,
            status=handle.status,
        )

    return start_flow


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
        turn=event.turn,
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
        events = await handle.events()
        return RunResponse(
            run_id=handle.run_id,
            status=current_status,
            event_count=len(events),
            artifacts=_artifact_responses(events),
        )
    try:
        result = await handle.wait()
    except Exception as exc:
        events = await handle.events()
        return RunResponse(
            run_id=handle.run_id,
            status=RunStatus.FAILED,
            event_count=len(events),
            error=f"{type(exc).__name__}: {exc}",
            artifacts=_artifact_responses(events),
        )
    return RunResponse(
        run_id=handle.run_id,
        status=result.status,
        event_count=len(await handle.events()),
        output=result.output,
        text=result.text,
        error=result.error,
        artifacts=tuple(_artifact_response(artifact) for artifact in result.artifacts),
    )


async def _flow_response(
    handle: FlowRunHandle[Any],
) -> FlowResponse:
    current_status = handle.status
    if current_status is FlowStatus.RUNNING:
        running_events = await handle.events()
        if handle.status is FlowStatus.RUNNING:
            return FlowResponse(
                flow_run_id=handle.flow_run_id,
                plan_id=_plan_id(running_events),
                status=current_status,
                event_count=len(running_events),
                artifacts=_flow_artifact_responses(running_events),
            )
    try:
        result = await handle.wait()
    except Exception as exc:
        failed_events = await handle.events()
        return FlowResponse(
            flow_run_id=handle.flow_run_id,
            plan_id=_plan_id(failed_events),
            status=FlowStatus.FAILED,
            event_count=len(failed_events),
            error=f"{type(exc).__name__}: {exc}",
            artifacts=_flow_artifact_responses(failed_events),
        )
    events = await handle.events()
    if isinstance(result, PlannerReActResult):
        return FlowResponse(
            flow_run_id=handle.flow_run_id,
            plan_id=result.plan_id,
            status=result.status,
            event_count=len(events),
            output=result.output,
            error=result.error,
            plan=(
                result.plan.model_dump(mode="json")
                if result.plan is not None
                else None
            ),
            child_runs=tuple(
                FlowChildRunResponse(
                    role=child.role.value,
                    phase=child.phase,
                    agent_run_id=child.agent_run_id,
                    status=child.run.status,
                    source_plan_revision=child.source_plan_revision,
                    target_plan_revision=child.target_plan_revision,
                    step_id=child.step_id,
                    attempt=child.attempt,
                )
                for child in result.child_runs
            ),
            artifacts=tuple(
                _artifact_response(artifact) for artifact in result.artifacts
            ),
        )
    if isinstance(result, FlowResult):
        return FlowResponse(
            flow_run_id=handle.flow_run_id,
            status=result.status,
            event_count=len(events),
            output=result.output,
            error=result.error,
            artifacts=tuple(
                _artifact_response(artifact)
                for artifact in result.artifacts
            ),
            child_runs=tuple(
                FlowChildRunResponse(
                    role="stage",
                    phase=stage.name,
                    agent_run_id=stage.run.run_id,
                    status=stage.run.status,
                    source_plan_revision=None,
                    target_plan_revision=None,
                    step_id=None,
                    attempt=stage.attempt,
                )
                for stage in (result.stage_attempts or result.stages)
            ),
        )
    return FlowResponse(
        flow_run_id=handle.flow_run_id,
        status=result.status,
        event_count=len(events),
        output=getattr(result, "output", None),
        error=getattr(result, "error", None),
    )


def _plan_id(events: tuple[Event, ...]) -> str | None:
    for event in events:
        plan_id = event.payload.get("plan_id")
        if isinstance(plan_id, str):
            return plan_id
    return None


def _flow_artifact_responses(
    events: tuple[Event, ...],
) -> tuple[ArtifactRefResponse, ...]:
    event_kind = "artifact.selected" if _plan_id(events) is not None else "artifact.created"
    return _artifact_responses(events, event_kind=event_kind)


def _artifact_responses(
    events: tuple[Event, ...],
    *,
    event_kind: str = "artifact.created",
) -> tuple[ArtifactRefResponse, ...]:
    responses: dict[str, ArtifactRefResponse] = {}
    for event in events:
        if event.kind != event_kind:
            continue
        artifact = ArtifactRefResponse(
            id=_optional_string(event.payload.get("artifact_id")),
            name=str(event.payload.get("name", "")),
            location=_optional_string(event.payload.get("location")),
            media_type=str(event.payload.get("media_type", "application/octet-stream")),
            size_bytes=_optional_int(event.payload.get("size_bytes")),
            sha256=_optional_string(event.payload.get("sha256")),
            created_at=_optional_datetime(event.payload.get("created_at")),
            producer_run_id=_optional_string(event.payload.get("producer_run_id")),
            producer_agent_id=_optional_string(event.payload.get("producer_agent_id")),
            flow_run_id=_optional_string(event.payload.get("flow_run_id")),
            stage=_optional_string(event.payload.get("stage")),
            tool_call_id=_optional_string(event.payload.get("tool_call_id")),
            source_artifact_ids=_string_tuple(
                event.payload.get("source_artifact_ids")
            ),
        )
        responses[artifact.id or artifact.name] = artifact
    return tuple(responses.values())


def _artifact_response(artifact: Any) -> ArtifactRefResponse:
    return ArtifactRefResponse(
        id=artifact.id,
        name=artifact.name,
        location=artifact.location,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        sha256=artifact.sha256,
        created_at=artifact.created_at,
        producer_run_id=artifact.run_id,
        producer_agent_id=artifact.agent_id,
        flow_run_id=artifact.flow_run_id,
        stage=artifact.stage,
        tool_call_id=artifact.tool_call_id,
        source_artifact_ids=artifact.source_artifact_ids,
    )


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))
