"""FastAPI control-plane routes for MCP OAuth authorization."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from base_agent.mcp.authorization import MCPAuthorizationManager

MCPRequestAuthorizer = Callable[[Request], None | Awaitable[None]]


class MCPAuthorizationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connection_name: str
    status: str
    authorization_url: str | None = None
    tool_count: int | None = None


def create_mcp_authorization_router(
    manager: MCPAuthorizationManager,
    *,
    prefix: str = "/v1/mcp",
    request_authorizer: MCPRequestAuthorizer | None = None,
) -> APIRouter:
    """Build opt-in routes; the Host remains responsible for endpoint auth."""

    router = APIRouter(prefix=prefix, tags=["mcp"])

    @router.post(
        "/connections/{connection_name}/authorize",
        response_model=MCPAuthorizationResponse,
    )
    async def authorize(
        connection_name: str,
        request: Request,
    ) -> MCPAuthorizationResponse:
        await _authorize_request(request, request_authorizer)
        try:
            result = await manager.start(connection_name)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"MCP authorization failed: {type(exc).__name__}",
            ) from exc
        return MCPAuthorizationResponse(
            connection_name=result.connection_name,
            status=result.status,
            authorization_url=result.authorization_url,
        )

    @router.get("/oauth/callback", response_model=MCPAuthorizationResponse)
    async def oauth_callback(
        state: Annotated[str, Query(min_length=1)],
        code: str | None = None,
        error: str | None = None,
    ) -> MCPAuthorizationResponse:
        try:
            result = await manager.complete(code=code, state=state, error=error)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"MCP authorization callback failed: {type(exc).__name__}",
            ) from exc
        return MCPAuthorizationResponse(
            connection_name=result.connection_name,
            status="authorized",
            tool_count=len(result.tools),
        )

    @router.get(
        "/connections/{connection_name}",
        response_model=MCPAuthorizationResponse,
    )
    async def status(
        connection_name: str,
        request: Request,
    ) -> MCPAuthorizationResponse:
        await _authorize_request(request, request_authorizer)
        try:
            available = await manager.token_available(connection_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return MCPAuthorizationResponse(
            connection_name=connection_name,
            status="authorized" if available else "authorization_required",
        )

    return router


async def _authorize_request(
    request: Request,
    authorizer: MCPRequestAuthorizer | None,
) -> None:
    if authorizer is None:
        return
    result = authorizer(request)
    if inspect.isawaitable(result):
        await result
