"""FastAPI adapter over the framework-neutral Agent facade and store ports."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse

from base_agent.agent import Agent
from base_agent.models import AgentResult, Artifact, Run, RuntimeEvent
from base_agent.server.schemas import ResumeRunRequest, StartRunRequest, StartRunResponse
from base_agent.server.tasks import RunTaskManager
from base_agent.stores import EventStream
from base_agent.stores.errors import (
    ArtifactNotFoundError,
    AttachmentNotFoundError,
    RunNotCancellableError,
    RunNotFoundError,
)


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

    @app.post(
        f"{route_prefix}/runs",
        response_model=StartRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def start_run(payload: StartRunRequest) -> StartRunResponse:
        try:
            attachments = tuple(
                [
                    await agent.artifact_store.get_attachment(attachment_id)
                    for attachment_id in payload.attachment_ids
                ]
            )
            handle = await tasks.start(
                payload.prompt,
                skills=payload.skills,
                attachments=attachments,
                plan=payload.plan,
            )
            run = await handle.get_run()
        except AttachmentNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return StartRunResponse(run_id=handle.run_id, status=run.status)

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
