"""Starter-owned MCP configuration, OAuth state, and cached Tool composition."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base_agent.mcp import (
    FileMCPToolManifestStore,
    FileOAuthTokenStorage,
    MCPAuthorizationManager,
    MCPAuthType,
    MCPServerConfig,
    MCPTool,
    OAuthTokenStorage,
    load_mcp_server_configs,
    tools_from_manifest,
)
from base_agent.mcp.server import MCPRequestAuthorizer, create_mcp_authorization_router
from fastapi import APIRouter

from agent_app.config import Settings


@dataclass(frozen=True, slots=True)
class MCPRuntime:
    configs: tuple[MCPServerConfig, ...]
    manifests: FileMCPToolManifestStore
    authorization: MCPAuthorizationManager
    state_root: Path

    def tools(self) -> tuple[MCPTool, ...]:
        resolved: list[MCPTool] = []
        for config in self.configs:
            if not config.enabled:
                continue
            resolved.extend(
                tools_from_manifest(
                    config,
                    self.manifests,
                    oauth_storage=self.storage_for(config),
                )
            )
        return tuple(resolved)

    def storage_for(self, config: MCPServerConfig) -> OAuthTokenStorage | None:
        if config.auth.type is not MCPAuthType.OAUTH:
            return None
        return FileOAuthTokenStorage(
            self.state_root / "oauth",
            connection_name=config.name,
        )

    def router(
        self,
        *,
        request_authorizer: MCPRequestAuthorizer | None = None,
    ) -> APIRouter:
        return create_mcp_authorization_router(
            self.authorization,
            request_authorizer=request_authorizer,
        )

    async def close(self) -> None:
        await self.authorization.close()


def build_mcp_runtime(settings: Settings) -> MCPRuntime | None:
    if settings.mcp_config_path is None:
        return None
    configs = tuple(
        config for config in load_mcp_server_configs(settings.mcp_config_path) if config.enabled
    )
    names = [config.name for config in configs]
    if len(names) != len(set(names)):
        raise ValueError("MCP config contains duplicate server names")
    manifests = FileMCPToolManifestStore(settings.mcp_state_root / "manifests")

    def storage_factory(config: MCPServerConfig) -> OAuthTokenStorage | None:
        if config.auth.type is not MCPAuthType.OAUTH:
            return None
        return FileOAuthTokenStorage(
            settings.mcp_state_root / "oauth",
            connection_name=config.name,
        )

    async def cache_tools(
        config: MCPServerConfig,
        tools: tuple[MCPTool, ...],
    ) -> None:
        await manifests.save(config, tools)

    authorization = MCPAuthorizationManager(
        {config.name: config for config in configs},
        storage_factory=storage_factory,
        on_authorized=cache_tools,
    )
    runtime = MCPRuntime(
        configs=configs,
        manifests=manifests,
        authorization=authorization,
        state_root=settings.mcp_state_root,
    )
    return runtime
