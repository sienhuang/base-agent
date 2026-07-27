"""FastAPI adapter over the framework-neutral Agent facade and store ports."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.middleware.base import RequestResponseEndpoint

from base_agent._logging import reset_log_context, set_log_context
from base_agent.agent import Agent
from base_agent.models import (
    AgentResult,
    Artifact,
    Conversation,
    ConversationTurn,
    Message,
    Run,
    RuntimeEvent,
)
from base_agent.server.schemas import (
    CreateConversationRequest,
    ResumeRunRequest,
    StartRunRequest,
    StartRunResponse,
)
from base_agent.server.tasks import RunTaskManager
from base_agent.stores import EventStream
from base_agent.stores.errors import (
    ArtifactNotFoundError,
    AttachmentNotFoundError,
    ConversationBusyError,
    ConversationNotFoundError,
    ConversationProfileMismatchError,
    RunNotCancellableError,
    RunNotFoundError,
)

logger = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def create_app(
    agent: Agent,
    *,
    prefix: str = "/v1",
    title: str = "base-agent Run Server",
    expose_artifact_content: bool = False,
) -> FastAPI:
    """Create an application-scoped HTTP/SSE adapter for one configured Agent."""

    if not prefix.startswith("/") or (len(prefix) > 1 and prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and must not end with '/'")
    route_prefix = "" if prefix == "/" else prefix
    tasks = RunTaskManager(agent)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        del application
        yield
        await tasks.close()

    app = FastAPI(title=title, lifespan=lifespan)
    app.state.agent = agent
    app.state.run_tasks = tasks

    @app.middleware("http")
    async def log_request(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        supplied_request_id = request.headers.get("X-Request-ID", "")
        request_id = (
            supplied_request_id
            if _REQUEST_ID.fullmatch(supplied_request_id)
            else str(uuid4())
        )
        log_tokens = set_log_context(request_id=request_id)
        started_at = monotonic()
        try:
            response = await call_next(request)
        except Exception as exc:
            logger.exception(
                "http request failed",
                extra={
                    "event": "http.request.failed",
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": round((monotonic() - started_at) * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
            raise
        else:
            response.headers["X-Request-ID"] = request_id
            logger.info(
                "http request completed",
                extra={
                    "event": "http.request.completed",
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((monotonic() - started_at) * 1000, 3),
                },
            )
            return response
        finally:
            reset_log_context(log_tokens)

    async def start_run_from_payload(
        payload: StartRunRequest,
        *,
        conversation_id: UUID | None = None,
    ) -> StartRunResponse:
        resolved_conversation_id = conversation_id or payload.conversation_id
        if (
            conversation_id is not None
            and payload.conversation_id is not None
            and conversation_id != payload.conversation_id
        ):
            raise HTTPException(
                status_code=422,
                detail="path and payload conversation_id must match",
            )
        try:
            attachments = tuple(
                [
                    await agent.artifact_store.get_attachment(attachment_id)
                    for attachment_id in payload.attachment_ids
                ]
            )
            handle = await tasks.start(
                payload.prompt,
                conversation_id=resolved_conversation_id,
                skills=payload.skills,
                attachments=attachments,
                plan=payload.plan,
            )
            run = await handle.get_run()
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (ConversationBusyError, ConversationProfileMismatchError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StartRunResponse(
            run_id=handle.run_id,
            status=run.status,
            conversation_id=run.conversation_id,
            turn_sequence=run.turn_sequence,
        )

    @app.post(
        f"{route_prefix}/conversations",
        response_model=Conversation,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_conversation(payload: CreateConversationRequest) -> Conversation:
        return await agent.create_conversation(metadata=payload.metadata)

    @app.get(
        f"{route_prefix}/conversations/{{conversation_id}}",
        response_model=Conversation,
    )
    async def get_conversation(conversation_id: UUID) -> Conversation:
        try:
            return await agent.get_conversation(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(
        f"{route_prefix}/conversations/{{conversation_id}}/turns",
        response_model=tuple[ConversationTurn, ...],
    )
    async def list_conversation_turns(
        conversation_id: UUID,
    ) -> tuple[ConversationTurn, ...]:
        try:
            return await agent.conversation_turns(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get(
        f"{route_prefix}/conversations/{{conversation_id}}/messages",
        response_model=tuple[Message, ...],
    )
    async def list_conversation_messages(
        conversation_id: UUID,
    ) -> tuple[Message, ...]:
        try:
            return await agent.conversation_messages(conversation_id)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post(
        f"{route_prefix}/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(payload: StartRunRequest) -> StartRunResponse:
        return await start_run_from_payload(payload)

    @app.post(
        f"{route_prefix}/conversations/{{conversation_id}}/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_conversation_run(
        conversation_id: UUID,
        payload: StartRunRequest,
    ) -> StartRunResponse:
        return await start_run_from_payload(payload, conversation_id=conversation_id)

    @app.get(f"{route_prefix}/runs/{{run_id}}", response_model=Run)
    async def get_run(run_id: UUID) -> Run:
        return await _get_run(agent, run_id)

    @app.post(f"{route_prefix}/runs/{{run_id}}/cancel", response_model=Run)
    async def cancel_run(run_id: UUID) -> Run:
        try:
            return await agent.cancel(run_id)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RunNotCancellableError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(f"{route_prefix}/runs/{{run_id}}/resume", response_model=AgentResult)
    async def resume_run(run_id: UUID, payload: ResumeRunRequest) -> AgentResult:
        try:
            return await tasks.resume(run_id, payload.input)
        except RunNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get(
        f"{route_prefix}/runs/{{run_id}}/events",
        response_model=tuple[RuntimeEvent, ...],
    )
    async def list_events(
        run_id: UUID,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
    ) -> tuple[RuntimeEvent, ...]:
        await _get_run(agent, run_id)
        events = await agent.events(run_id)
        return tuple(event for event in events if event.sequence > after_sequence)

    @app.get(f"{route_prefix}/runs/{{run_id}}/events/stream")
    async def stream_events(
        run_id: UUID,
        after_sequence: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> Response:
        await _get_run(agent, run_id)
        if not isinstance(agent.event_store, EventStream):
            raise HTTPException(
                status_code=501,
                detail="configured EventStore does not support live subscriptions",
            )
        cursor = _event_cursor(after_sequence, last_event_id)
        stream = agent.event_store.subscribe(run_id, after_sequence=cursor)
        return StreamingResponse(
            _sse_events(stream),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get(
        f"{route_prefix}/runs/{{run_id}}/artifacts",
        response_model=tuple[Artifact, ...],
    )
    async def list_artifacts(run_id: UUID) -> tuple[Artifact, ...]:
        await _get_run(agent, run_id)
        return await agent.list_artifacts(run_id)

    @app.get(
        f"{route_prefix}/runs/{{run_id}}/artifacts/{{artifact_id}}",
        response_model=Artifact,
    )
    async def get_artifact(run_id: UUID, artifact_id: UUID) -> Artifact:
        await _get_run(agent, run_id)
        return await _artifact_for_run(agent, run_id, artifact_id)

    if expose_artifact_content:

        @app.get(
            f"{route_prefix}/runs/{{run_id}}/artifacts/{{artifact_id}}/content"
        )
        async def get_artifact_content(run_id: UUID, artifact_id: UUID) -> Response:
            await _get_run(agent, run_id)
            artifact = await _artifact_for_run(agent, run_id, artifact_id)
            content = await agent.read_content(artifact.id)
            filename = quote(artifact.name, safe="")
            return Response(
                content=content,
                media_type=artifact.media_type,
                headers={
                    "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    return app


async def _get_run(agent: Agent, run_id: UUID) -> Run:
    try:
        return await agent.get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _artifact_for_run(agent: Agent, run_id: UUID, artifact_id: UUID) -> Artifact:
    try:
        artifact = await agent.get_artifact(artifact_id)
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if artifact.run_id != run_id:
        raise HTTPException(status_code=404, detail="artifact was not found for this Run")
    return artifact


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


async def _sse_events(events: AsyncIterator[RuntimeEvent]) -> AsyncIterator[bytes]:
    async for event in events:
        data = json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        yield f"id: {event.sequence}\nevent: {event.type.value}\ndata: {data}\n\n".encode()
