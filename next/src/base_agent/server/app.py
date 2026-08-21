"""FastAPI routes over the public Agent and RunHandle APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query, Request, status
from sse_starlette import EventSourceResponse, ServerSentEvent
from starlette.types import Lifespan

from base_agent.agent import Agent
from base_agent.checkpoints import (
    CheckpointClaimError,
    CheckpointConflictError,
    CheckpointNotFoundError,
)
from base_agent.events import EventStore, EventStream
from base_agent.execution import (
    ExecutionAttempt,
    ExecutionRequestConflictError,
    ExecutionStore,
)
from base_agent.flows import (
    FlowResult,
    FlowRunHandle,
    FlowRunNotCancellableError,
    FlowStatus,
    PlannerReActResult,
)
from base_agent.governance import ToolConfirmation
from base_agent.models import Event, RunResult, RunStatus
from base_agent.run_handle import (
    EventStreamingNotSupportedError,
    RunHandle,
    RunNotCancellableError,
)
from base_agent.run_store import (
    AgentRunRecord,
    InMemoryRunStore,
    RunLeaseConflictError,
    RunLeaseLostError,
    RunRecordNotFoundError,
    RunStore,
    RunTransitionError,
)
from base_agent.server.queued_executor import QueuedRunExecutor
from base_agent.server.recovery import (
    ExecutionKind,
    is_execution_history,
    project_flow_response,
    project_run_response,
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
    ConfirmRunRequest,
    EventResponse,
    FlowChildRunResponse,
    FlowResponse,
    PendingInputResponse,
    RecoveryAttemptResponse,
    RecoveryDecisionRequest,
    RecoveryResponse,
    ResumeRunRequest,
    RunResponse,
    StartFlowRequest,
    StartFlowResponse,
    StartRunRequest,
    StartRunResponse,
)

OperatorAuthorizer = Callable[[Request, str], None | Awaitable[None]]


def create_http_app(
    agent: Agent | None = None,
    *,
    prefix: str = "/v1",
    title: str = "base-agent Run Server",
    sse_ping_seconds: int = 15,
    flows: Mapping[str, RegisteredFlow] | None = None,
    lifespan: Lifespan[FastAPI] | None = None,
    event_store: EventStore | None = None,
    run_store: RunStore | None = None,
    run_lease_seconds: float = 30.0,
    run_heartbeat_interval_seconds: float | None = None,
    runtime_build_id: str | None = None,
    execution_store: ExecutionStore | None = None,
    execution_mode: str = "local",
    runtime_route: str = "default",
    operator_authorizer: OperatorAuthorizer | None = None,
) -> FastAPI:
    """Create HTTP routes with local or independently queued Agent execution."""
    if not prefix.startswith("/") or (len(prefix) > 1 and prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and must not end with '/'")
    if sse_ping_seconds < 1:
        raise ValueError("sse_ping_seconds must be at least 1")
    route_prefix = "" if prefix == "/" else prefix
    if agent is None and not flows:
        raise ValueError("create_http_app requires at least one Agent or Flow")
    if execution_mode not in {"local", "queued"}:
        raise ValueError("execution_mode must be local or queued")
    if execution_mode == "queued" and (run_store is None or execution_store is None):
        raise ValueError("queued execution requires run_store and execution_store")
    resolved_flows = dict(flows or {})
    _validate_event_store_ownership(event_store, agent, resolved_flows)
    resolved_run_store = run_store or (InMemoryRunStore() if agent is not None else None)
    queued_executor = (
        QueuedRunExecutor(
            agent,
            resolved_run_store,
            execution_store,
            runtime_route=runtime_route,
            runtime_build_id=runtime_build_id,
        )
        if agent is not None
        and resolved_run_store is not None
        and execution_store is not None
        and execution_mode == "queued"
        else None
    )
    registry = (
        InMemoryRunRegistry(
            agent,
            resolved_run_store,
            lease_seconds=run_lease_seconds,
            heartbeat_interval_seconds=run_heartbeat_interval_seconds,
            runtime_build_id=runtime_build_id,
            executor=queued_executor,
        )
        if agent is not None and resolved_run_store is not None
        else None
    )
    flow_registry = InMemoryFlowRegistry(resolved_flows)
    recovery_store = event_store or _common_event_store(agent, resolved_flows)
    @asynccontextmanager
    async def execution_lifespan(application: FastAPI) -> AsyncIterator[None]:
        @asynccontextmanager
        async def execution_scope() -> AsyncIterator[None]:
            if registry is not None:
                await registry.executor.startup()
            try:
                yield
            finally:
                if registry is not None:
                    await registry.executor.close()

        if lifespan is None:
            async with execution_scope():
                yield
            return
        async with lifespan(application):
            async with execution_scope():
                yield

    app = FastAPI(title=title, lifespan=execution_lifespan)
    app.state.run_registry = registry
    app.state.flow_registry = flow_registry
    app.state.event_store = recovery_store
    app.state.run_store = resolved_run_store
    app.state.execution_store = execution_store
    app.state.sse_ping_seconds = sse_ping_seconds

    if registry is not None:
        _register_run_routes(
            app,
            registry,
            event_store=recovery_store,
            route_prefix=route_prefix,
            sse_ping_seconds=sse_ping_seconds,
            operator_authorizer=operator_authorizer,
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
            continued = flow_registry.continued_result(flow_run_id)
            if continued is not None:
                events = await _persisted_events(
                    recovery_store,
                    flow_run_id,
                    kind="flow",
                )
                return _flow_result_response(flow_run_id, continued, events)
            handle = _find_flow_handle(flow_registry, flow_run_id)
            if handle is not None:
                return await _flow_response(handle)
            events = await _persisted_events(
                recovery_store,
                flow_run_id,
                kind="flow",
            )
            return project_flow_response(flow_run_id, events)

        @app.post(
            f"{route_prefix}/flows/{{flow_run_id}}/resume",
            response_model=FlowResponse,
        )
        async def resume_flow(
            flow_run_id: str,
            payload: ResumeRunRequest,
        ) -> FlowResponse:
            try:
                result = await flow_registry.resume(flow_run_id, payload.input)
            except FlowRunNotFoundError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (CheckpointClaimError, CheckpointConflictError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            events = await _persisted_events(
                recovery_store,
                flow_run_id,
                kind="flow",
            )
            return _flow_result_response(flow_run_id, result, events)

        @app.post(
            f"{route_prefix}/flows/{{flow_run_id}}/confirm",
            response_model=FlowResponse,
        )
        async def confirm_flow(
            flow_run_id: str,
            payload: ConfirmRunRequest,
        ) -> FlowResponse:
            try:
                result = await flow_registry.confirm(
                    flow_run_id,
                    ToolConfirmation(**payload.model_dump()),
                )
            except FlowRunNotFoundError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (CheckpointClaimError, CheckpointConflictError, ValueError) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            events = await _persisted_events(
                recovery_store,
                flow_run_id,
                kind="flow",
            )
            return _flow_result_response(flow_run_id, result, events)

        @app.post(
            f"{route_prefix}/flows/{{flow_run_id}}/cancel",
            response_model=FlowResponse,
        )
        async def cancel_flow(flow_run_id: str) -> FlowResponse:
            handle = _find_flow_handle(flow_registry, flow_run_id)
            if handle is None:
                await _persisted_events(
                    recovery_store,
                    flow_run_id,
                    kind="flow",
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Flow Run '{flow_run_id}' is not owned by this server process "
                        "and cannot be cancelled"
                    ),
                )
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
            handle = _find_flow_handle(flow_registry, flow_run_id)
            if handle is None:
                events = await _persisted_events(
                    recovery_store,
                    flow_run_id,
                    kind="flow",
                )
                return tuple(
                    _event_response(event)
                    for event in events
                    if event.sequence > after_sequence
                )
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
            cursor = _event_cursor(
                after_sequence,
                last_event_id_header or last_event_id_query,
            )
            handle = _find_flow_handle(flow_registry, flow_run_id)
            if handle is not None:
                try:
                    events = handle.stream(after_sequence=cursor)
                except EventStreamingNotSupportedError as exc:
                    raise HTTPException(status_code=501, detail=str(exc)) from exc
            else:
                await _persisted_events(
                    recovery_store,
                    flow_run_id,
                    kind="flow",
                )
                events = _persisted_stream(
                    recovery_store,
                    flow_run_id,
                    after_sequence=cursor,
                )
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
    event_store: EventStore | None,
    route_prefix: str,
    sse_ping_seconds: int,
    operator_authorizer: OperatorAuthorizer | None,
) -> None:
    @app.post(
        f"{route_prefix}/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(
        payload: StartRunRequest,
        idempotency_key: Annotated[
            str | None,
            Header(alias="Idempotency-Key", min_length=1, max_length=256),
        ] = None,
    ) -> StartRunResponse:
        try:
            record = await registry.start(
                payload.prompt,
                stream=payload.stream,
                idempotency_key=idempotency_key,
            )
        except ExecutionRequestConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return StartRunResponse(run_id=record.run_id, status=record.status)

    @app.get(
        f"{route_prefix}/runs/{{run_id}}",
        response_model=RunResponse,
    )
    async def get_run(run_id: str) -> RunResponse:
        try:
            record = await registry.record(run_id)
        except RunRecordNotFoundError:
            events = await _persisted_events(event_store, run_id, kind="run")
            return project_run_response(run_id, events)
        return await _run_record_response(
            record,
            event_store,
            recovery_attempt=await registry.latest_attempt(run_id),
        )

    @app.post(
        f"{route_prefix}/runs/{{run_id}}/resume",
        response_model=RunResponse,
    )
    async def resume_run(run_id: str, payload: ResumeRunRequest) -> RunResponse:
        try:
            record = await registry.resume(run_id, payload.input)
        except (CheckpointNotFoundError, RunRecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            CheckpointClaimError,
            CheckpointConflictError,
            RunLeaseConflictError,
            RunLeaseLostError,
            RunTransitionError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _run_record_response(record, event_store)

    @app.post(
        f"{route_prefix}/operator/runs/{{run_id}}/recovery-decisions",
        response_model=RunResponse,
    )
    async def decide_run_recovery(
        request: Request,
        run_id: str,
        payload: RecoveryDecisionRequest,
    ) -> RunResponse:
        if operator_authorizer is None:
            raise HTTPException(
                status_code=403,
                detail="Operator recovery authorization is not configured",
            )
        authorization = operator_authorizer(request, payload.subject_id)
        if isinstance(authorization, Awaitable):
            await authorization
        try:
            record, _ = await registry.decide_recovery(
                run_id,
                decision=payload.decision,
                strategy=payload.strategy,
                subject_id=payload.subject_id,
                reason_code=payload.reason_code,
                expected_revision=payload.expected_revision,
                idempotency_key=payload.idempotency_key,
            )
        except RunRecordNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            ExecutionRequestConflictError,
            RunTransitionError,
            RunNotFoundError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _run_record_response(
            record,
            event_store,
            recovery_attempt=await registry.latest_attempt(run_id),
        )

    @app.post(
        f"{route_prefix}/runs/{{run_id}}/confirm",
        response_model=RunResponse,
    )
    async def confirm_run(run_id: str, payload: ConfirmRunRequest) -> RunResponse:
        try:
            record = await registry.confirm(
                run_id,
                ToolConfirmation(**payload.model_dump()),
            )
        except (CheckpointNotFoundError, RunRecordNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (
            CheckpointClaimError,
            CheckpointConflictError,
            RunLeaseConflictError,
            RunLeaseLostError,
            RunTransitionError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _run_record_response(record, event_store)

    @app.post(
        f"{route_prefix}/runs/{{run_id}}/cancel",
        response_model=RunResponse,
    )
    async def cancel_run(run_id: str) -> RunResponse:
        try:
            record = await registry.cancel(run_id)
        except RunRecordNotFoundError as exc:
            try:
                await _persisted_events(event_store, run_id, kind="run")
            except HTTPException:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Run '{run_id}' is not owned by this server process and "
                    "predates persistent Run control state; it cannot be cancelled"
                ),
            ) from exc
        except (
            CheckpointNotFoundError,
            CheckpointClaimError,
            CheckpointConflictError,
            RunLeaseConflictError,
            RunLeaseLostError,
            RunTransitionError,
            RunNotCancellableError,
            ValueError,
        ) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await _run_record_response(record, event_store)

    @app.get(
        f"{route_prefix}/runs/{{run_id}}/events",
        response_model=tuple[EventResponse, ...],
    )
    async def list_events(
        run_id: str,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[EventResponse, ...]:
        """Replay the persisted Event history strictly after one cursor."""
        handle = _find_handle(registry, run_id)
        if handle is None:
            try:
                await registry.record(run_id)
            except RunRecordNotFoundError:
                events = await _persisted_events(event_store, run_id, kind="run")
            else:
                events = () if event_store is None else await event_store.list(run_id)
            return tuple(
                _event_response(event)
                for event in events
                if event.sequence > after_sequence
            )
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
        cursor = _event_cursor(
            after_sequence,
            last_event_id_header or last_event_id_query,
        )
        handle = _find_handle(registry, run_id)
        if handle is not None:
            try:
                events = handle.stream(after_sequence=cursor)
            except EventStreamingNotSupportedError as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
        else:
            try:
                await registry.record(run_id)
            except RunRecordNotFoundError:
                await _persisted_events(event_store, run_id, kind="run")
            events = _persisted_stream(
                event_store,
                run_id,
                after_sequence=cursor,
            )
        return EventSourceResponse(
            _sse_events(events),
            ping=sse_ping_seconds,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _find_handle(registry: InMemoryRunRegistry, run_id: str) -> RunHandle | None:
    try:
        return registry.get(run_id)
    except RunNotFoundError:
        return None


def _find_flow_handle(
    registry: InMemoryFlowRegistry,
    flow_run_id: str,
) -> FlowRunHandle[Any] | None:
    try:
        return registry.get(flow_run_id)
    except FlowRunNotFoundError:
        return None


async def _persisted_events(
    event_store: EventStore | None,
    execution_id: str,
    *,
    kind: ExecutionKind,
) -> tuple[Event, ...]:
    events = () if event_store is None else await event_store.list(execution_id)
    if events and is_execution_history(events, kind):
        return events
    label = "Run" if kind == "run" else "Flow Run"
    raise HTTPException(status_code=404, detail=f"{label} '{execution_id}' was not found")


def _persisted_stream(
    event_store: EventStore | None,
    execution_id: str,
    *,
    after_sequence: int,
) -> AsyncIterator[Event]:
    if not isinstance(event_store, EventStream):
        raise HTTPException(
            status_code=501,
            detail="the configured EventStore does not support live subscriptions",
        )
    return event_store.subscribe(execution_id, after_sequence=after_sequence)


def _common_event_store(
    agent: Agent | None,
    flows: Mapping[str, RegisteredFlow],
) -> EventStore | None:
    candidates: list[EventStore] = []
    if agent is not None:
        candidates.append(agent.event_store)
    for flow in flows.values():
        store = getattr(flow, "event_store", None)
        if isinstance(store, EventStore):
            candidates.append(store)
    if not candidates:
        return None
    first = candidates[0]
    return first if all(candidate is first for candidate in candidates) else None


def _validate_event_store_ownership(
    event_store: EventStore | None,
    agent: Agent | None,
    flows: Mapping[str, RegisteredFlow],
) -> None:
    if event_store is None:
        return
    if agent is not None and agent.event_store is not event_store:
        raise ValueError("HTTP event_store must be the Agent's EventStore")
    for name, flow in flows.items():
        flow_store = getattr(flow, "event_store", None)
        if flow_store is not None and flow_store is not event_store:
            raise ValueError(
                f"HTTP event_store must be Flow '{name}' EventStore"
            )


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
        pending_input=_pending_response(result),
    )


async def _run_result_response(
    result: RunResult,
    event_store: EventStore | None,
) -> RunResponse:
    events = () if event_store is None else await event_store.list(result.run_id)
    return RunResponse(
        run_id=result.run_id,
        status=result.status,
        event_count=len(events),
        output=result.output,
        text=result.text,
        error=result.error,
        artifacts=tuple(_artifact_response(artifact) for artifact in result.artifacts),
        pending_input=_pending_response(result),
    )


async def _run_record_response(
    record: AgentRunRecord,
    event_store: EventStore | None,
    *,
    recovery_attempt: ExecutionAttempt | None = None,
) -> RunResponse:
    events = () if event_store is None else await event_store.list(record.run_id)
    pending = (
        PendingInputResponse.model_validate(record.pending_input)
        if record.pending_input is not None
        else None
    )
    return RunResponse(
        run_id=record.run_id,
        status=record.status,
        event_count=len(events),
        output=record.output,
        text=record.text,
        error=record.error,
        artifacts=_artifact_responses(events),
        pending_input=pending,
        recovery=_recovery_response(record, recovery_attempt),
    )


def _recovery_response(
    record: AgentRunRecord,
    attempt: ExecutionAttempt | None,
) -> RecoveryResponse | None:
    if record.status not in {RunStatus.RECOVERY_REQUIRED, RunStatus.BLOCKED}:
        return None
    if record.recovery_reason_code is None or record.recovery_detected_at is None:
        return None
    last_attempt = (
        RecoveryAttemptResponse(
            attempt_id=attempt.attempt_id,
            status=attempt.status.value,
            owner_id=attempt.owner_id,
            runtime_build_id=attempt.runtime_build_id,
            heartbeat_at=attempt.heartbeat_at,
            lease_expires_at=attempt.lease_expires_at,
            reason_code=attempt.reason_code,
        )
        if attempt is not None
        else None
    )
    return RecoveryResponse(
        reason_code=record.recovery_reason_code,
        revision=record.revision,
        detected_at=record.recovery_detected_at,
        last_attempt=last_attempt,
    )


def _pending_response(
    result: RunResult,
) -> PendingInputResponse | None:
    pending = result.pending_input
    if pending is None:
        return None
    return PendingInputResponse(
        request_id=pending.request_id,
        kind=pending.kind,
        tool_call_id=pending.tool_call_id,
        tool_name=pending.tool_name,
        prompt=pending.prompt,
        metadata=dict(pending.metadata),
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
    return _flow_result_response(handle.flow_run_id, result, events)


def _flow_result_response(
    flow_run_id: str,
    result: Any,
    events: tuple[Event, ...],
) -> FlowResponse:
    if isinstance(result, PlannerReActResult):
        return FlowResponse(
            flow_run_id=flow_run_id,
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
            **_flow_pending_response_arguments(result),
        )
    if isinstance(result, FlowResult):
        return FlowResponse(
            flow_run_id=flow_run_id,
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
            **_flow_pending_response_arguments(result),
        )
    return FlowResponse(
        flow_run_id=flow_run_id,
        status=result.status,
        event_count=len(events),
        output=getattr(result, "output", None),
        error=getattr(result, "error", None),
    )


def _flow_pending_response_arguments(result: Any) -> dict[str, Any]:
    pending = getattr(result, "pending_input", None)
    child_run_id = getattr(result, "waiting_child_run_id", None)
    values: dict[str, Any] = {}
    if pending is not None:
        values["pending_input"] = PendingInputResponse(
            request_id=pending.request_id,
            kind=pending.kind,
            tool_call_id=pending.tool_call_id,
            tool_name=pending.tool_name,
            prompt=pending.prompt,
            metadata=dict(pending.metadata),
        )
    if child_run_id is not None:
        values["waiting_child_run_id"] = child_run_id
    return values


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
