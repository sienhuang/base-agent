# Execution-scoped Resources

Resources connect an Agent to stateful capabilities such as browser sessions, sandboxes, MCP
clients, database transactions, or temporary workspaces. The core manages their lifecycle but does
not implement those capabilities.

Define a resource as an asynchronous context manager and register it with the Agent:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from base_agent import ResourceSpec, RuntimeContext

@asynccontextmanager
async def browser(context: RuntimeContext) -> AsyncIterator[Browser]:
    active = await Browser.launch(run_id=context.run_id)
    try:
        yield active
    finally:
        await active.close()

agent = Agent(
    profile=profile,
    model=model,
    tools=(open_page,),
    resources=(ResourceSpec("browser", browser),),
)
```

Resources are lazy by default. Set `eager=True` when the Run must fail before orchestration if a
resource cannot be acquired. Each named resource is acquired at most once per execution segment,
and acquired resources are released in reverse order in the same asynchronous task.

## Using resources from Tools

A Tool requests the hidden `ToolContext`; it is not included in the schema shown to the model:

```python
from base_agent import ToolContext, tool

@tool
async def open_page(url: str, context: ToolContext) -> str:
    browser = await context.resources.get("browser", Browser)
    return await browser.open(url)
```

This is the main integration point for Skills: a Skill selects and instructs the model to use an
atomic Tool, while the Tool obtains its browser, sandbox, MCP client, or domain session from the
current resource scope. Neither the Skill nor the model receives a live infrastructure object.

Custom orchestration strategies use the same manager through
`services.resources.get("name", ExpectedType)`.

## Waiting and resume

`WAITING` ends the current execution coroutine, so all acquired resources are released before
`run.waiting`. Resuming the same Run creates a new execution segment and reacquires resources from
the same `ResourceSpec` definitions.

Never serialize a live client into `RuntimeCheckpoint`. If an external session must survive a
wait, persist a serializable session identifier in application storage and make the resource
factory reconnect using the Run ID or checkpointed domain state.

## Failures and cancellation

- Partial acquisition failure releases all resources already acquired.
- Strategy and Tool failures still release resources.
- Direct task cancellation runs cleanup before propagating `CancelledError`.
- Acquisition and release failures emit `resource.failed` events.
- Release failures do not hide a successful agent output; they are recorded in Run and Result
  `resource_failures` metadata for application policy to handle.

The terminal Run event is emitted after resource cleanup, so replay consumers see a complete
lifecycle before `run.completed`, `run.failed`, or `run.waiting`.
