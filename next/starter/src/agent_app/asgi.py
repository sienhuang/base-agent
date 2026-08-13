"""Thin ASGI composition root over the SDK-owned HTTP adapter."""

from __future__ import annotations

import argparse

import uvicorn
from base_agent.server import create_http_app
from fastapi import FastAPI

from agent_app.app import build_agent, build_plan_react_flow
from agent_app.config import Settings
from agent_app.provider import build_plan_react_providers, build_provider


def create_app() -> FastAPI:
    """Build the Starter Agent and host it with ``base_agent.server``."""
    settings = Settings.from_env()
    provider = build_provider(settings)
    planner_provider, executor_provider = build_plan_react_providers(
        settings,
        default_provider=provider,
    )
    agent = build_agent(provider)
    plan_react = build_plan_react_flow(
        provider,
        planner_provider=planner_provider,
        executor_provider=executor_provider,
        planner_model=settings.plan_react_planner_model,
        executor_model=settings.plan_react_executor_model,
    )
    return create_http_app(
        agent,
        title="base-agent Starter",
        flows={"plan-react": plan_react},
    )


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Starter FastAPI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(app, host=arguments.host, port=arguments.port)
