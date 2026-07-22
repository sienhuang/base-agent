# Background Runs and Events

`Agent.run()` waits for the final result. Use `Agent.start()` when an application needs to observe
or control the Run while it executes:

```python
handle = await agent.start("Complete the task")

async for event in handle.stream():
    print(event.sequence, event.type)

result = await handle.result()
```

`Agent.start()` creates an `asyncio.Task` in the caller's event loop and returns only after the Run
record exists. `RunHandle` is deliberately independent of FastAPI, SSE, Redis, and task queues.
Applications may map it onto those transports without changing Agent or Runtime code.

## Handle operations

- `result()` waits for the `AgentResult`. Cancelling this waiter does not cancel the Run.
- `cancel()` requests cooperative cancellation through `RunStore`.
- `get_run()` returns the latest Run snapshot.
- `events()` returns the currently persisted event history.
- `stream()` replays persisted events and follows new events until the Run completes, fails, is
  cancelled, reaches a limit, or waits for input.

## Cursor replay

Every event has a per-Run sequence number. Resume after the last delivered event without receiving
duplicates:

```python
async for event in handle.stream(after_sequence=last_sequence):
    last_sequence = event.sequence
```

The default `InMemoryEventStore` implements the optional `EventStream` protocol. A PostgreSQL,
Redis, or other EventStore can expose the same subscription contract. Stores that implement only
history listing remain valid `EventStore` implementations, but `RunHandle.stream()` raises
`EventStreamingNotSupportedError` for them.

## Process boundary

`RunHandle` is an in-process control object. It does not claim that an `asyncio.Task` survives a
process restart. Durable servers should persist Runs and Events, schedule work through an external
runner, and reconstruct API responses from the store ports. That infrastructure stays outside the
core execution loop.

## Wait for human input and resume

A Tool requests input by returning `WaitForInput`. No Tool name is special-cased:

```python
from base_agent import WaitForInput, tool

@tool
async def ask_user(question: str) -> WaitForInput:
    return WaitForInput(prompt=question)
```

When the model calls this Tool, the Runtime:

1. emits `tool.waiting`;
2. changes the Run to `WAITING`;
3. stores a `RuntimeCheckpoint` through `CheckpointStore`;
4. exposes the pending prompt through Run and Result metadata;
5. emits `run.waiting` and returns an `AgentResultStatus.WAITING` result.

Resume the same Run with the user's answer:

```python
from uuid import UUID

waiting = await agent.run("Build the report")
prompt = waiting.metadata["pending_input"]["prompt"]

answer = await collect_answer(prompt)
run_id = UUID(str(waiting.metadata["run_id"]))
completed = await agent.resume(run_id, answer)
```

The answer becomes the result of the original pending Tool call, preserving provider message
ordering. The restored Runtime retains its messages, token usage, selected Skills, enabled Tools,
step/tool budgets, and Supervisor state. A Run may wait and resume more than once.

Checkpoint claims are atomic: only one concurrent resume can continue a waiting Run. Empty input
does not consume the checkpoint. Cancelling a waiting Run immediately finalizes it and removes the
checkpoint.

`InMemoryCheckpointStore` is intended for local use and tests. Applications that must resume after
a process restart should supply a durable `CheckpointStore` alongside durable Run and Event stores.
