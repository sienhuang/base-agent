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

## Safety notes

- Do not put a whole workflow in one Tool.
- Do not pass secrets through Tool descriptions or model arguments.
- Use narrow permissions such as `orders:read` instead of broad labels.
- Sync functions run in a worker thread and cannot be force-killed after a timeout; prefer async
  clients with their own cancellation support for external I/O.
