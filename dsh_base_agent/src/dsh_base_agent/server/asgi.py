"""Real DSH-backed FastAPI composition root and CLI."""

from __future__ import annotations

import argparse
from dataclasses import replace

import uvicorn
from fastapi import FastAPI

from dsh_base_agent.application import DshApplication
from dsh_base_agent.context import CompactingContextPolicy, Context, KeepAllContextPolicy
from dsh_base_agent.definitions import AgentDefinition
from dsh_base_agent.providers import ProviderConfig
from dsh_base_agent.server.config import (
    DshAgentDefinition,
    DshFastApiSettings,
    load_dsh_agent_registry,
)


def create_dsh_app(settings: DshFastApiSettings | None = None) -> FastAPI:
    resolved = settings or DshFastApiSettings.from_env()
    default_agent_name, definitions = _agent_definitions(resolved)
    provider_profiles = {
        name: ProviderConfig(
            name=f"agent.{name}",
            provider=definition.provider,
            model=definition.model,
            reasoning_effort=resolved.reasoning_effort,
            max_tokens=resolved.max_tokens,
        )
        for name, definition in definitions.items()
    }
    service = DshApplication(
        workspace=resolved.workspace,
        state_dir=resolved.state_dir,
        providers=provider_profiles.values(),
        default_provider=provider_profiles[default_agent_name].name,
        default_context=Context(name="service", policy=_context_policy(resolved)),
        dsh_bin=resolved.dsh_bin,
        initialize_timeout_seconds=resolved.initialize_timeout_seconds,
        request_timeout_seconds=resolved.request_timeout_seconds,
        shutdown_timeout_seconds=resolved.shutdown_timeout_seconds,
    )
    for name, definition in definitions.items():
        service.register_agent(
            AgentDefinition(
                id=name,
                version="1.0.0",
                instructions=definition.instructions,
            ),
            context="service",
            provider=provider_profiles[name].name,
            default=name == default_agent_name,
        )
    application = service.fastapi(default_agent=default_agent_name)
    application.state.settings = resolved
    return application


def _agent_definitions(
    settings: DshFastApiSettings,
) -> tuple[str, dict[str, DshAgentDefinition]]:
    if settings.agent_config_path is not None:
        registry = load_dsh_agent_registry(settings.agent_config_path)
        return registry.default_agent, registry.agents
    return settings.agent_name, {
        settings.agent_name: DshAgentDefinition(
            instructions=settings.instructions,
            provider=settings.provider,
            model=settings.model,
        )
    }


def _context_policy(
    settings: DshFastApiSettings,
) -> KeepAllContextPolicy | CompactingContextPolicy:
    if settings.context_policy == "keep-all":
        return KeepAllContextPolicy()
    return CompactingContextPolicy(
        threshold_ratio=settings.compaction_threshold_ratio,
        retain_ratio=settings.compaction_retain_ratio,
    )


def main() -> None:
    settings = DshFastApiSettings.from_env()
    parser = argparse.ArgumentParser(description="Run the real DSH Base Agent FastAPI server.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65_535:
        parser.error("--port must be between 1 and 65535")
    settings = replace(settings, host=arguments.host, port=arguments.port)
    uvicorn.run(
        create_dsh_app(settings),
        host=arguments.host,
        port=arguments.port,
    )


if __name__ == "__main__":
    main()
