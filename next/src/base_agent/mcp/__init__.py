"""Optional MCP client integration; install ``base-agent-next[mcp]`` first."""

from base_agent.mcp.authorization import (
    MCPAuthorizationComplete,
    MCPAuthorizationManager,
    MCPAuthorizationStart,
)
from base_agent.mcp.client import MCPClient, mcp_tool_name
from base_agent.mcp.config import (
    MCPAuthConfig,
    MCPAuthType,
    MCPServerConfig,
    MCPToolPolicyConfig,
    MCPTransport,
    load_mcp_server_configs,
)
from base_agent.mcp.connection import MCPConnection
from base_agent.mcp.manifest import (
    FileMCPToolManifestStore,
    MCPOnDemandSession,
    mcp_config_fingerprint,
    tools_from_manifest,
)
from base_agent.mcp.storage import (
    FileOAuthTokenStorage,
    InMemoryOAuthTokenStorage,
    OAuthTokenStorage,
)
from base_agent.mcp.tool import MCPTool, MCPToolCallError

__all__ = [
    "FileOAuthTokenStorage",
    "FileMCPToolManifestStore",
    "InMemoryOAuthTokenStorage",
    "MCPAuthConfig",
    "MCPAuthType",
    "MCPAuthorizationComplete",
    "MCPAuthorizationManager",
    "MCPAuthorizationStart",
    "MCPClient",
    "MCPConnection",
    "MCPOnDemandSession",
    "MCPServerConfig",
    "MCPTool",
    "MCPToolCallError",
    "MCPToolPolicyConfig",
    "MCPTransport",
    "OAuthTokenStorage",
    "load_mcp_server_configs",
    "mcp_config_fingerprint",
    "mcp_tool_name",
    "tools_from_manifest",
]
