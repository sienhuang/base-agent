"""Validated MCP server, authentication, and local Tool policy configuration."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.governance import Confirmation, SideEffect


class MCPTransport(StrEnum):
    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable_http"


class MCPAuthType(StrEnum):
    NONE = "none"
    HEADERS = "headers"
    BEARER = "bearer"
    OAUTH = "oauth"


class MCPAuthConfig(BaseModel):
    """Authentication for a remote Streamable HTTP MCP server."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: MCPAuthType = MCPAuthType.NONE
    headers: dict[str, str] = Field(default_factory=dict)
    token_env: str | None = None
    redirect_uri: str | None = None
    scopes: tuple[str, ...] = ()
    client_name: str = "base-agent"
    client_uri: str | None = None
    client_metadata_url: str | None = None
    authorization_timeout_seconds: float = 300.0

    @model_validator(mode="after")
    def validate_auth(self) -> MCPAuthConfig:
        if self.authorization_timeout_seconds <= 0:
            raise ValueError("MCP OAuth authorization timeout must be positive")
        if self.type is MCPAuthType.BEARER and not self.token_env:
            raise ValueError("MCP bearer authentication requires token_env")
        if self.type is MCPAuthType.OAUTH:
            if self.redirect_uri is None:
                raise ValueError("MCP OAuth authentication requires redirect_uri")
            _require_http_url(self.redirect_uri, "MCP OAuth redirect_uri")
            if self.client_uri is not None:
                _require_http_url(self.client_uri, "MCP OAuth client_uri")
            if self.client_metadata_url is not None:
                parsed = urlparse(self.client_metadata_url)
                if parsed.scheme != "https" or not parsed.netloc or parsed.path in {"", "/"}:
                    raise ValueError(
                        "MCP OAuth client_metadata_url must be an HTTPS URL with a path"
                    )
        elif self.redirect_uri is not None or self.scopes or self.client_metadata_url is not None:
            raise ValueError("MCP OAuth fields require auth.type='oauth'")
        if self.type is not MCPAuthType.HEADERS and self.headers:
            raise ValueError("MCP auth headers require auth.type='headers'")
        return self


class MCPToolPolicyConfig(BaseModel):
    """Host-owned policy applied to one discovered MCP Tool."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    permissions: frozenset[str] | None = None
    timeout_seconds: float | None = None
    side_effect: SideEffect | None = None
    confirmation: Confirmation | None = None

    @model_validator(mode="after")
    def validate_policy(self) -> MCPToolPolicyConfig:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("MCP Tool timeout must be positive")
        if (
            self.confirmation is Confirmation.REQUIRED
            and self.side_effect is SideEffect.READ_ONLY
        ):
            raise ValueError("read-only MCP Tools must not require confirmation")
        return self


class MCPServerConfig(BaseModel):
    """One MCP server connection plus optional local governance overrides."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    transport: MCPTransport
    enabled: bool = True
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    auth: MCPAuthConfig = Field(default_factory=MCPAuthConfig)
    connect_timeout_seconds: float = 30.0
    read_timeout_seconds: float | None = None
    tool_timeout_seconds: float = 30.0
    default_permissions: frozenset[str] | None = None
    default_side_effect: SideEffect = SideEffect.UNKNOWN
    default_confirmation: Confirmation = Confirmation.NONE
    trust_read_only_annotations: bool = False
    supports_parallel_tool_calls: bool = False
    max_artifact_bytes: int = 16 * 1024 * 1024
    tools: dict[str, MCPToolPolicyConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_server(self) -> MCPServerConfig:
        if self.connect_timeout_seconds <= 0 or self.tool_timeout_seconds <= 0:
            raise ValueError("MCP connection and Tool timeouts must be positive")
        if self.read_timeout_seconds is not None and self.read_timeout_seconds <= 0:
            raise ValueError("MCP read timeout must be positive")
        if self.max_artifact_bytes < 1:
            raise ValueError("MCP max_artifact_bytes must be positive")
        if self.transport is MCPTransport.STDIO:
            if not self.command:
                raise ValueError("stdio MCP server requires command")
            if self.url is not None:
                raise ValueError("stdio MCP server must not define url")
            if self.headers:
                raise ValueError("stdio MCP server must not define HTTP headers")
            if self.auth.type is not MCPAuthType.NONE:
                raise ValueError("stdio MCP server authentication belongs in its env")
        else:
            if self.url is None:
                raise ValueError("Streamable HTTP MCP server requires url")
            _require_http_url(self.url, "MCP server url")
            if self.command is not None or self.args or self.cwd is not None or self.env:
                raise ValueError("Streamable HTTP MCP server must not define stdio fields")
        if (
            self.default_confirmation is Confirmation.REQUIRED
            and self.default_side_effect is SideEffect.READ_ONLY
        ):
            raise ValueError("read-only MCP Tools must not require confirmation")
        return self

    @property
    def resolved_default_permissions(self) -> frozenset[str]:
        if self.default_permissions is not None:
            return self.default_permissions
        return frozenset({f"mcp:{self.name}:invoke"})

    def policy_for(self, remote_tool_name: str) -> MCPToolPolicyConfig:
        return self.tools.get(remote_tool_name, MCPToolPolicyConfig())


def load_mcp_server_configs(path: str | Path) -> tuple[MCPServerConfig, ...]:
    """Load the conventional ``mcp_servers`` mapping from YAML."""

    source = Path(path)
    payload = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("MCP config root must be an object")
    raw_servers = payload.get("mcp_servers", payload)
    if not isinstance(raw_servers, dict):
        raise ValueError("mcp_servers must be an object")
    configs: list[MCPServerConfig] = []
    for name, raw in raw_servers.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("each MCP server must be a named object")
        item: dict[str, Any] = {"name": name, **raw}
        configs.append(MCPServerConfig.model_validate(item))
    return tuple(configs)


def _require_http_url(value: str, field_name: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} must be an HTTP(S) URL")
