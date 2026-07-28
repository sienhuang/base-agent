# ReAct on the Shared Model/Tool Loop

ReAct is an opt-in behavior layered on `ModelToolStrategy`; it is not a second Tool runtime.
Providers, Tool permissions, Supervisor decisions, cancellation, token usage, logs, Run Events,
and WAITING checkpoints therefore keep the same contracts as a normal Run.

## Run a whole task with ReAct

```python
from base_agent import AgentRuntime, ReActStrategy

agent = Agent(
    profile=profile,
    model=model,
    tools=tools,
    runtime=AgentRuntime(
        strategy=ReActStrategy(max_iterations=20),
    ),
)

result = await agent.run("Inspect the service and report the cause")
```

The normal `agent.run(prompt)` path still uses `ModelToolStrategy` without ReAct events or a
structured completion requirement.

## Run one Plan Step with ReAct

```python
plan = ExecutionPlan(
    id="repair",
    title="Repair service",
    steps=(
        PlanStep(
            id="inspect",
            description="Inspect the failing service",
            executor="react",
        ),
        PlanStep(
            id="report",
            description="Write the final report",
            executor="model",
            dependencies=("inspect",),
        ),
    ),
)
```

`executor="react"` enables ReAct only for that Step. `executor="model"` and an omitted executor
retain the existing Plan Step behavior. `PlanningStrategy` defaults to 20 ReAct model iterations
per Step; configure `max_react_iterations_per_step` to change that bound.

## Iterations and Action batches

One iteration has this observable shape:

```text
model request
  → action batch selected
  → Tool calls executed in model-returned order
  → observation batch recorded
  → next model request
```

The model may return multiple independent Tool calls in one ActionBatch. The current Runtime
executes them sequentially in the returned order, preserving the existing `ModelToolStrategy`
behavior and provider message protocol. A Tool call whose arguments depend on an earlier result
must be selected in a later iteration. Parallel Tool execution is not implied.

If a Tool returns WAITING, the checkpoint stores the waiting call, completed observations, and
remaining calls. Resume records the user input as that call's result and continues the unexecuted
calls without repeating the model request or already completed Tools.

## Completion contract

A standalone ReAct task and a ReAct Plan Step finish with JSON:

```json
{
  "success": true,
  "result": "Concrete result",
  "attachments": []
}
```

For a Plan Step, these fields are stored on the immutable completed `PlanStep`, together with its
ReAct iteration count. A malformed completion fails the Run instead of silently accepting an
ambiguous Step result.

## Observability

ReAct adds these Events:

- `react.iteration.started`
- `react.action_batch.selected`
- `react.observation_batch.recorded`
- `react.iteration.completed`

Existing model and Tool Events are still emitted. ReAct Events contain iteration, Tool identity,
status, and Plan/Step correlation where applicable. They intentionally do not expose or persist
private model chain-of-thought.
