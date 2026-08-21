"""Thin ASGI composition root over the SDK-owned HTTP adapter."""

from __future__ import annotations

import argparse
import hmac

import uvicorn
from base_agent import AwaitHumanConfirmations, LocalArtifactStore
from base_agent.server import AgentApplication, OperatorAuthorizer
from fastapi import FastAPI, HTTPException, Request

from agent_app.app import build_agent, build_plan_react_flow
from agent_app.config import Settings
from agent_app.events import build_event_store_runtime
from agent_app.provider import build_plan_react_providers, build_provider


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the Starter Agent and host it with ``base_agent.server``."""
    resolved_settings = settings or Settings.from_env()
    if (
        resolved_settings.execution_mode == "postgres-worker"
        and resolved_settings.operator_token is None
    ):
        raise ValueError(
            "AGENT_OPERATOR_TOKEN is required by the API in postgres-worker mode"
        )
    provider = build_provider(resolved_settings)
    planner_provider, executor_provider = build_plan_react_providers(
        resolved_settings,
        default_provider=provider,
    )
    artifact_store = LocalArtifactStore(resolved_settings.artifact_root)
    event_runtime = build_event_store_runtime(resolved_settings)
    agent = build_agent(
        provider,
        artifact_store=artifact_store,
        event_store=event_runtime.store,
        checkpoint_store=event_runtime.checkpoints,
        confirmation_policy=AwaitHumanConfirmations(),
    )
    plan_react = build_plan_react_flow(
        provider,
        planner_provider=planner_provider,
        executor_provider=executor_provider,
        planner_model=resolved_settings.plan_react_planner_model,
        executor_model=resolved_settings.plan_react_executor_model,
        artifact_store=artifact_store,
        event_store=event_runtime.store,
        checkpoint_store=event_runtime.checkpoints,
        confirmation_policy=AwaitHumanConfirmations(),
    )
    application = AgentApplication(
        agent=agent,
        title="base-agent Starter",
        flows={"plan-react": plan_react},
        preflight=(event_runtime.start,),
        shutdown=(event_runtime.close,),
        event_store=event_runtime.store,
        run_store=event_runtime.runs,
        run_lease_seconds=resolved_settings.run_lease_seconds,
        run_heartbeat_interval_seconds=resolved_settings.run_heartbeat_interval_seconds,
        runtime_build_id=resolved_settings.runtime_build_id,
        execution_store=event_runtime.executions,
        execution_mode=(
            "queued"
            if resolved_settings.execution_mode == "postgres-worker"
            else "local"
        ),
        runtime_route=resolved_settings.runtime_route,
        operator_authorizer=_operator_authorizer(resolved_settings.operator_token),
    ).fastapi()
    return application


def _operator_authorizer(token: str | None) -> OperatorAuthorizer | None:
    if token is None:
        return None

    def authorize(request: Request, subject_id: str) -> None:
        del subject_id
        authorization = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=403, detail="invalid Operator credentials")

    return authorize


app = create_app()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Starter FastAPI server.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(app, host=arguments.host, port=arguments.port)
