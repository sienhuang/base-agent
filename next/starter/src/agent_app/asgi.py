"""Thin ASGI composition root over the SDK-owned HTTP adapter."""

from __future__ import annotations

import argparse

import uvicorn
from base_agent.server import create_http_app
from fastapi import FastAPI

from agent_app.app import build_agent
from agent_app.config import Settings
from agent_app.provider import build_provider


def create_app() -> FastAPI:
    """Build the Starter Agent and host it with ``base_agent.server``."""
    settings = Settings.from_env()
    agent = build_agent(build_provider(settings))
    return create_http_app(agent, title="base-agent Starter")


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Starter FastAPI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(app, host=arguments.host, port=arguments.port)
