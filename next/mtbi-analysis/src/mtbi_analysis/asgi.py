"""FastAPI composition root for the MTBI Analysis Flow."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from base_agent.providers import ModelProvider
from base_agent.server import create_http_app
from fastapi import FastAPI

from mtbi_analysis.app import build_application
from mtbi_analysis.config import Settings
from mtbi_analysis.runner import MTBICommandRunner
from mtbi_analysis.tools import check_mtbi_environment


def create_app(
    settings: Settings | None = None,
    *,
    provider: ModelProvider | None = None,
    runner: MTBICommandRunner | None = None,
) -> FastAPI:
    """Build the independent MTBI application on the SDK HTTP adapter."""
    application = build_application(settings, provider=provider, runner=runner)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await check_mtbi_environment(application.workspace, application.runner)
        yield

    return create_http_app(
        application.flow.query_agent,
        title="base-agent MTBI Analysis",
        flows={"mtbi-analysis": application.flow},
        lifespan=lifespan,
    )


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MTBI Analysis FastAPI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(app, host=arguments.host, port=arguments.port)
