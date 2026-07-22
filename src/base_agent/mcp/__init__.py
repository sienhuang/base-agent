"""Optional MCP Tool adapter; install base-agent[mcp] before importing."""

from mcp import StdioServerParameters

from base_agent.mcp.client import MCPClient
from base_agent.mcp.tool import MCPTool, MCPToolCallError
from base_agent.mcp.transports import stdio_mcp_client, streamable_http_mcp_client

__all__ = [
    "MCPClient",
    "MCPTool",
    "MCPToolCallError",
    "StdioServerParameters",
    "stdio_mcp_client",
    "streamable_http_mcp_client",
]
