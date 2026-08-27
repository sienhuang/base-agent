# MCP Client

`base-agent-next[mcp]` consumes third-party MCP servers. Authentication belongs to the
connection; every discovered remote Tool is adapted to the ordinary base-agent `Tool` protocol
and therefore continues through Harness validation, timeout, Event, Artifact, and recovery paths.

```text
MCPConnection (transport + auth + ClientSession)
  └─ MCPClient (initialize + paginated tools/list)
       ├─ MCPTool mcp__github__search
       └─ MCPTool mcp__github__create_issue
```

## Install

```bash
uv sync --project next --extra mcp
```

The root `base_agent` import does not load the MCP SDK. Import `base_agent.mcp` only when the
extra is installed. FastAPI OAuth routes additionally require the `server` extra.

## Direct SDK use

```python
from base_agent.mcp import MCPConnection, MCPServerConfig, MCPTransport

config = MCPServerConfig(
    name="math",
    transport=MCPTransport.STDIO,
    command="python",
    args=("/opt/mcp/math_server.py",),
)

async with MCPConnection(config) as connection:
    tools = await connection.client.tools()
    agent = Agent(
        definition_with_mcp_tool_names_and_permissions,
        model=model,
        tools=tools,
    )
    result = await agent.run("Calculate the value")
```

The context owns the subprocess or HTTP transport and must remain open for every invocation of
these session-backed Tools. It also guarantees that the anyio transport context is opened and
closed in the same asyncio Task.

## Authentication

Streamable HTTP supports four modes:

```yaml
auth: {type: none}
```

```yaml
auth:
  type: headers
  headers:
    X-API-Key: configured-secret
```

```yaml
auth:
  type: bearer
  token_env: GITHUB_MCP_TOKEN
```

```yaml
auth:
  type: oauth
  redirect_uri: http://127.0.0.1:8001/v1/mcp/oauth/callback
  client_name: base-agent
  scopes: [repo, read:user]
```

OAuth uses the official MCP SDK's protected-resource discovery, authorization-server metadata,
dynamic client registration, PKCE, Token exchange, and refresh flow. `OAuthTokenStorage` is a
Host boundary. The included `FileOAuthTokenStorage` uses atomic files with mode `0600`, but does
not encrypt them; distributed or production Hosts should replace it with a shared database or
secret manager implementation.

The FastAPI control plane is opt-in:

```python
app.include_router(create_mcp_authorization_router(manager))
```

It provides:

- `POST /v1/mcp/connections/{name}/authorize`
- `GET /v1/mcp/oauth/callback`
- `GET /v1/mcp/connections/{name}`

The Host must authenticate the authorize and status routes. The callback is bound by the OAuth
state value and remains reachable by the user's browser.

## Server/Worker snapshots

The Starter stores the Tool schema returned after authorization/discovery in a fingerprinted
manifest. API, Worker supervisor, and isolated execution child read the same manifest, producing
the same Agent snapshot. The child opens an authenticated connection only when an MCP Tool is
actually invoked; it never launches an interactive OAuth flow.

The included coordinator keeps the pending browser handshake in the API process. Run one API
process for authorization, or configure sticky routing so the callback returns to that process.
API and Workers on different hosts must share both Token storage and manifest storage.

After first authorization or a remote Tool-list change, restart API and Worker processes to load
the new manifest into a new Agent snapshot. A Run never mutates its Tool list in the middle of
execution.

## Results and recovery

- MCP `isError=true` becomes a normal Tool execution error.
- Text and structured content remain JSON values.
- Image, audio, and binary embedded-resource blocks are committed through
  `ToolContext.artifacts`; the Tool result contains an Artifact receipt instead of base64.
- Protocol `_meta` is removed before content reaches the model.
- The default side effect is `unknown`. This does not block normal invocation, but prevents an
  ambiguous timeout from being replayed automatically by recovery.
- A Host can override permissions, side effect, confirmation, and timeout per remote Tool.
- Calls to one server inside one Agent process are serialized unless
  `supports_parallel_tool_calls: true` is explicitly configured. Independent execution children
  can still call the remote server concurrently; reduce Worker concurrency when a server cannot
  tolerate that.

OAuth scopes authorize the installation/user against the remote service. Local side-effect data
has a different purpose: it determines whether a failed or timed-out operation can be recovered
safely.
