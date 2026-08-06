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

## Classify side effects

Side-effect policy is implementation metadata and is not exposed in the model-facing Tool schema.
Existing Tools default to `UNSPECIFIED` for backward compatibility; production applications should
classify them deliberately:

```python
from base_agent import ToolContext, ToolSideEffectMode, tool


@tool(side_effect=ToolSideEffectMode.READ_ONLY)
async def get_order(order_id: str) -> dict[str, str]:
    return await orders.get(order_id)


@tool(side_effect=ToolSideEffectMode.UNSAFE)
async def send_notification(recipient: str, body: str) -> str:
    return await notifications.send(recipient, body)


@tool(side_effect=ToolSideEffectMode.IDEMPOTENT)
async def charge_order(
    order_id: str,
    cents: int,
    context: ToolContext,
) -> dict[str, str]:
    # The downstream service, not only base-agent, must enforce this key.
    return await payments.charge(
        order_id=order_id,
        cents=cents,
        idempotency_key=context.idempotency_key,
    )
```

The modes mean:

- `UNSPECIFIED`: legacy behavior; no safety claim and no automatic ledger record;
- `READ_ONLY`: explicitly promises no external mutation;
- `UNSAFE`: external mutation without a downstream replay guarantee;
- `IDEMPOTENT`: external mutation protected by a downstream idempotency key.

An `IDEMPOTENT` `FunctionTool` must accept `ToolContext`. Declaring the mode without sending
`context.idempotency_key` to a downstream system does not make the operation idempotent.

### Connect Flow execution evidence

Flow applications can connect governed Tools to the same ledger used by recovery:

```python
from base_agent import (
    Agent,
    DefinitionResolvingFlowWorkHandler,
    FlowToolSideEffectRecorder,
    InMemoryFlowSideEffectLedger,
)

ledger = InMemoryFlowSideEffectLedger()
recorder = FlowToolSideEffectRecorder(ledger)

agent = Agent(
    definition=agent_definition,
    model=model,
    tools=[get_order, charge_order],
    side_effect_recorder=recorder,
)

handler = DefinitionResolvingFlowWorkHandler(
    flow_repository,
    definition_resolver,
    recovery_dispatcher,
    side_effect_evidence=ledger,
)
```

`ToolExecutor` validates arguments and permissions before creating evidence. Immediately before
calling a side-effecting Tool it records PREPARED then STARTED. A normal return records CONFIRMED;
a timeout, exception, cancellation, or process loss leaves STARTED because the outcome is
uncertain. Reusing a STARTED/CONFIRMED operation is denied for `UNSAFE` Tools and allowed for
`IDEMPOTENT` Tools with the same generated key.

The operation identity is derived from the Flow Run, Invocation, and original Tool call ID.
Recovery dispatchers must resume the persisted Agent execution and original Tool call rather than
ask the model to invent a replacement call. A newly generated Tool call ID represents a new
operation and therefore receives a different downstream idempotency key.

The Flow ledger stores only correlation metadata and the SHA-256 digest of the key. It never stores
Tool arguments, results, exception text, or the original key.

## Require confirmation before important effects

Confirmation is a separate declaration from side-effect retry behavior:

```python
from base_agent import (
    ToolConfirmationMode,
    ToolSideEffectMode,
    tool,
)


@tool(
    side_effect=ToolSideEffectMode.UNSAFE,
    confirmation=ToolConfirmationMode.REQUIRED,
)
async def publish_release(version: str) -> str:
    return await releases.publish(version)
```

On the first call, `ToolExecutor` validates allowlists, permissions, and arguments, then returns a
`WAITING` result before creating side-effect evidence or calling the function. The pending input
contains a bounded confirmation request tied to the exact Run, Tool name, Tool call ID, and
arguments digest.

Use the typed confirmation API, not free-form resume text:

```python
from uuid import UUID

from base_agent import (
    ToolConfirmation,
    ToolConfirmationDecision,
)

pending = waiting.metadata["pending_input"]
request_id = UUID(pending["metadata"]["request"]["id"])

completed = await agent.confirm(
    run_id,
    ToolConfirmation(
        request_id=request_id,
        decision=ToolConfirmationDecision.APPROVE,
        subject_id=authenticated_operator_id,
        reason_code="release_ticket_approved",
    ),
)
```

Approval resumes the original persisted ToolCall, preserving its ID and downstream idempotency key.
It does not ask the model to construct another call and does not increment the logical Tool-call
count twice. Rejection never invokes the Tool or starts the side-effect ledger; instead it appends
a structured `tool_confirmation_rejected` result so the model can continue safely.

`Agent.resume()` rejects confirmation requests, and `Agent.confirm()` rejects ordinary input
requests. A mismatched request ID leaves the checkpoint available for a valid decision. Sequential
Flows expose the same split through `resume(...)` and `confirm(...)`.

`subject_id` and `reason_code` are audit fields, not authentication. The host must authenticate the
operator, enforce tenant/Run ownership, and authorize the specific effect before constructing the
decision. Confirmation events intentionally omit Tool arguments. Applications can replace
`DeclaredToolConfirmationPolicy` with a `ToolConfirmationPolicy` that produces an approved bounded
prompt or requires confirmation under stricter application rules.

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

Treat a Tool result as model context, not as bulk storage. The default model/tool strategy
serializes each non-waiting `ToolResult` into a `tool` message, so the Runtime enforces a serialized
UTF-8 JSON byte limit at the common `ToolExecutor` boundary.

The default limit is 262,144 bytes and can be pinned in an Agent definition:

```python
definition = AgentDefinition(
    id="bounded-report-agent",
    version="1.0.0",
    instructions="Return bounded summaries.",
    max_tool_result_bytes=64_000,
)
```

`ToolHarness(..., max_result_bytes=...)` uses the same policy. Applications needing a different
policy can inject a `ToolResultPolicy` into `Agent` or `ToolExecutor`.

When the complete serialized envelope exceeds the limit, `BoundedToolResultPolicy` replaces it
with a valid bounded `ToolResult`:

```json
{
  "status": "error",
  "error_code": "tool_result_too_large",
  "data": {
    "original_size_bytes": 900000,
    "limit_bytes": 262144,
    "original_status": "success",
    "overflow_action": "rejected"
  }
}
```

The original data and error text are excluded. JSON is never cut at an arbitrary byte boundary.
The replacement is what enters the model message, Event, Run snapshot, and any later Checkpoint.
UTF-8 bytes are measured rather than Python character count, so multibyte content is handled
correctly. If a side-effecting Tool completed, its ledger is confirmed before its oversized result
is replaced; loss of the observation does not make the external outcome unknown.

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

### Artifact overflow boundary

Automatic Artifact externalization is deliberately not inferred. A generic Runtime cannot know
whether an oversized value is safe to persist, which media type and retention policy apply, or
whether the model may receive a reference to it. Tool authors remain responsible for storing
approved bulk content through
`ToolContext.artifacts` and returning a bounded reference envelope.

## Safety notes

- Do not put a whole workflow in one Tool.
- Do not pass secrets through Tool descriptions or model arguments.
- Do not label a Tool `READ_ONLY` or `IDEMPOTENT` unless its implementation and downstream system
  enforce that claim.
- Do not treat a model response or arbitrary `resume()` text as operator authorization.
- Do not return unbounded collections, logs, documents, or command output directly to the model.
- Use narrow permissions such as `orders:read` instead of broad labels.
- Sync functions run in a worker thread and cannot be force-killed after a timeout; prefer async
  clients with their own cancellation support for external I/O.
