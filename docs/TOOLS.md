# Writing Tools

A Tool is one typed, atomic capability. Keep domain workflows in Skills and keep orchestration in
the Runtime.

## Define a Tool

```python
from base_agent import tool

@tool(permissions=frozenset({"orders:read"}), timeout_seconds=10)
async def get_order(order_id: str) -> dict[str, str]:
    """Get one order by ID."""
    return {"order_id": order_id, "status": "paid"}
```

The decorator derives the model-facing JSON Schema from parameter annotations and defaults.
Variadic, positional-only, and untyped parameters are rejected.

## Enable the Tool

The Tool must be both registered on the Agent and named in the profile:

```python
profile = AgentProfile(
    id="order-agent",
    instructions="Help with orders.",
    tools=("get_order",),
    permissions=frozenset({"orders:read"}),
)

agent = Agent(profile=profile, model=model, tools=[get_order])
```

This deliberate duplication prevents a registered administrative Tool from becoming model-visible
by accident.

## Test a Tool alone

```python
from base_agent.testing import ToolHarness

harness = ToolHarness([get_order])
result = await harness.run(
    "get_order",
    {"order_id": "10001"},
    permissions=frozenset({"orders:read"}),
)
assert result.succeeded
```

The Harness uses the same argument validation, permission checks, timeout, error normalization, and
JSON conversion as the Agent Runtime.

## Request human input

A Tool can suspend the current Run without relying on a reserved Tool name:

```python
from base_agent import WaitForInput, tool

@tool
async def confirm_change(summary: str) -> WaitForInput:
    return WaitForInput(
        prompt=f"Apply this change? {summary}",
        metadata={"kind": "approval"},
    )
```

The Runtime stores the pending Tool call in a checkpoint. `Agent.resume(run_id, answer)` completes
that exact call and continues the model/tool loop. See [Background Runs and Events](RUNS.md).

## Bound Tool results

Treat a Tool result as model context, not as bulk storage. The default model/tool strategy currently
serializes each non-waiting `ToolResult` into a `tool` message. An unbounded result therefore enters
every later model request in the Run and can exhaust the model context window, increase latency and
cost, and enlarge events and checkpoints.

Tool implementations must keep their returned JSON small and useful for the next reasoning step:

- return a summary, counts, important fields, and a bounded sample;
- store large text, binary content, query results, logs, and reports as Artifacts;
- return the Artifact ID, media type, size, and an explicit `truncated` or `has_more` indicator;
- expose bounded pagination, search, aggregation, or chunk-reading Tools when the model may need
  more of the stored content;
- apply explicit maximum values to `limit`, page size, chunk size, and similar arguments;
- redact secrets and sensitive data before either returning a result or creating an Artifact.

For example:

```python
import json

from base_agent import ToolContext, tool


@tool(permissions=frozenset({"orders:read"}))
async def query_orders(context: ToolContext) -> dict[str, object]:
    rows = await load_orders()
    artifact = await context.artifacts.create(
        name="orders.json",
        media_type="application/json",
        content=json.dumps(rows, ensure_ascii=False).encode(),
    )
    sample_size = 10
    return {
        "summary": "Order query completed.",
        "row_count": len(rows),
        "sample": rows[:sample_size],
        "artifact_id": str(artifact.id),
        "artifact_size_bytes": artifact.size_bytes,
        "truncated": len(rows) > sample_size,
    }
```

The Artifact is the data plane; the Tool result is the small control-plane message that tells the
model what happened and how to request more data.

### Planned Runtime enforcement

The Runtime still needs a provider-independent, configurable Tool-result size guard. The future
capability should:

1. measure the UTF-8 size, and optionally estimated token count, of the serialized `ToolResult`
   before appending a `tool` message;
2. apply a conservative default limit with per-Agent or Runtime configuration;
3. preserve valid structured JSON and never truncate a serialized JSON string blindly;
4. return a typed `tool_result_too_large` failure when the Tool did not externalize bulk content;
5. keep lifecycle events and checkpoints bounded as well as model messages;
6. record the original size, applied limit, Tool name, and overflow action without copying the
   oversized payload into telemetry;
7. cover success, failure, multibyte UTF-8, nested data, repeated model steps, and Artifact-reference
   behavior with deterministic tests.

Automatic Artifact externalization should only be added with an explicit serialization and access
policy. Until then, Tool authors are responsible for storing bulk content through
`ToolContext.artifacts` and returning a bounded reference envelope.

## Safety notes

- Do not put a whole workflow in one Tool.
- Do not pass secrets through Tool descriptions or model arguments.
- Do not return unbounded collections, logs, documents, or command output directly to the model.
- Use narrow permissions such as `orders:read` instead of broad labels.
- Sync functions run in a worker thread and cannot be force-killed after a timeout; prefer async
  clients with their own cancellation support for external I/O.
