# Optional MCP Tool Adapter

The MCP adapter discovers tools from an initialized Model Context Protocol session and maps each
one onto base-agent's existing `Tool` protocol. MCP does not replace `Agent`, `ToolExecutor`, Skills,
permissions, supervision, or Run events.

The project currently targets the official stable Python SDK v1 and pins `mcp<2`; the SDK's v2 API
is not treated as stable by this release.

## Install

```bash
uv add 'base-agent[mcp]'
```

Importing `base_agent` does not import the MCP SDK. Only `base_agent.mcp` requires this extra.

## stdio server

```python
import asyncio

from base_agent import Agent, AgentProfile
from base_agent.mcp import StdioServerParameters, stdio_mcp_client


async def main() -> None:
    parameters = StdioServerParameters(
        command="python",
        args=["/opt/my-service/mcp_server.py"],
    )
    async with stdio_mcp_client(parameters) as mcp:
        tools = await mcp.tools(
            name_prefix="analytics",
            permissions=frozenset({"mcp:analytics"}),
        )
        agent = Agent(
            profile=AgentProfile(
                id="analyst",
                instructions="Use approved analytics tools.",
                tools=tuple(tool.definition.name for tool in tools),
                permissions=frozenset({"mcp:analytics"}),
            ),
            model=model,
            tools=tools,
        )
        result = await agent.run("Calculate the current metric")


asyncio.run(main())
```

The context owns the child process, protocol streams, and `ClientSession`. Keep it open for every
Run or resume operation that may invoke its tools. A discovered `MCPTool` must not be retained after
the context closes.

## Streamable HTTP server

```python
from base_agent.mcp import streamable_http_mcp_client

async with streamable_http_mcp_client(
    "https://mcp.example.com/mcp",
    http_client=authenticated_http_client,
) as mcp:
    tools = await mcp.tools(name_prefix="catalog")
```

The host application owns HTTP authentication, OAuth flows, TLS policy, proxy configuration, and
the lifetime of an injected `httpx.AsyncClient`.

## Discovery and invocation

- Tool discovery follows every `nextCursor` page and returns an immutable snapshot.
- `name_prefix="analytics"` maps a remote `query` tool to `analytics.query`, avoiding collisions
  between servers while preserving the remote name for invocation.
- Remote input schemas are checked when tools are constructed. Arguments are validated locally
  against the MCP JSON Schema before network or subprocess execution.
- The default permission is `mcp:invoke`. `permissions` sets a server-wide policy and
  `permissions_by_tool` can replace it for sensitive tools.
- Tool execution still uses the base-agent timeout, allowlist, Skill, event, and supervision paths.
- MCP `isError` results become normal structured Tool execution errors rather than crashing a Run.
- Content blocks are converted to JSON-safe dictionaries. `structuredContent` is exposed as
  `structured_content`.
- Protocol `_meta` fields are deliberately removed before Tool results enter model context.

The current adapter intentionally does not implement dynamic `tools/list_changed` mutation during a
Run. Rediscovery creates a new Tool snapshot for subsequent Agent construction.

## Security boundary

Treat an MCP server and its tool descriptions as untrusted external capabilities:

- configure stdio commands and arguments explicitly; never construct them from model output;
- use name prefixes and explicit profile tool allowlists;
- grant permissions based on application policy, not MCP annotation hints;
- scope filesystem, network, database, and secret access in the MCP server process itself;
- do not place credentials in prompts, tool descriptions, committed configuration, or event data;
- authenticate and authorize remote HTTP servers outside the adapter.

This adapter supports MCP tools only. MCP resources, prompts, sampling, roots, and elicitation need
separate product decisions and are not automatically exposed to the model.
