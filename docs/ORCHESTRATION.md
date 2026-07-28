# Orchestration Strategies and Execution Plans

`AgentRuntime` owns the durable Run lifecycle. An `OrchestrationStrategy` owns one bounded turn of
execution. The default `ModelToolStrategy` implements the familiar model → tool → model loop, so
normal agents require no custom runtime code.

Use a custom strategy when an application needs planning, routing, or staged execution:

```python
from base_agent import AgentRuntime, ExecutionState, RuntimeContext, RuntimeServices

class DirectStrategy:
    async def advance(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        context.output = f"handled by {services.provider.name}"
        context.state_machine.transition_to(ExecutionState.COMPLETED)

runtime = AgentRuntime(strategy=DirectStrategy())
```

`RuntimeServices` supplies the provider, Tool registry/executor, Run/Event stores, and Supervisor.
It contains no application-specific objects. Each `advance()` call must perform bounded work and
either update the context or move it to a waiting or terminal state.

## Plans

`ExecutionPlan` and `PlanStep` are immutable, provider-neutral models. Step dependencies form an
acyclic graph; `ready_steps()` returns only pending steps whose dependencies are complete or
skipped. Transition methods return a new plan with a higher revision:

```python
from base_agent import ExecutionPlan, PlanStep

plan = ExecutionPlan(
    id="report",
    title="Build report",
    steps=(
        PlanStep(id="inspect", description="Inspect source data"),
        PlanStep(
            id="publish",
            description="Publish the report",
            dependencies=("inspect",),
        ),
    ),
)

plan = plan.start_step("inspect")
plan = plan.complete_step("inspect", result={"rows": 10})
```

The default Runtime automatically executes an application-supplied plan:

```python
result = await agent.run("Build the report", plan=plan)
```

Normal `agent.run(prompt)` behavior is unchanged. With a Plan, the built-in `PlanningStrategy`
selects the first ready Step, executes it through the same model → Tool loop, records its result,
and then asks the Planner model to regenerate all remaining work. The Runtime retains every
completed, failed, cancelled, or skipped Step unchanged and replaces only not-yet-executed Steps.
The merged graph is validated again before execution continues. An empty remaining `steps` array
ends execution, after which one Tool-free model call synthesizes the final answer.

This follows a Planner/ReAct lifecycle:

```text
PLANNING → EXECUTING_STEP → UPDATING_PLAN ─┐
                         ↑                 │
                         └─────────────────┘
                                      ↓
                                SUMMARIZING
```

The updating model call returns JSON shaped as `{"steps": [...]}`. Those Steps may add, remove,
split, merge, or reorder future work and may depend on retained historical Step IDs. They must
start pending, use IDs distinct from retained history, and form a valid acyclic graph. Returning
`{"steps": []}` means the original task is complete.

Generate and execute a Plan inside the same Run when the application explicitly requests it:

```python
result = await agent.run("Research and publish a report", planning=True)
```

The planning model call must return a JSON `ExecutionPlan`. It is validated for identifiers,
dependencies, cycles, initial statuses, and supported executors before any Step Tool runs. Planning,
Step execution, and final summarization all belong to the same Run, so cancellation, Events,
Provider usage, and actual token totals remain correlated.

The HTTP equivalent is:

```json
{
  "prompt": "Research and publish a report",
  "planning": true
}
```

`plan=` and `planning=True` are mutually exclusive. The built-in strategy currently executes Steps
sequentially and accepts `executor=null`, `"model"`, or `"react"`; all three use the normal
permissioned model/Tool loop. A `"react"` Step additionally enables observable iterations,
multi-Tool ActionBatch/ObservationBatch events, a structured Step result, and a per-Step iteration
limit. Unknown executor names fail before model or Tool execution. Parallel Steps, parallel Tool
execution, custom executor registries, and retry/skip policies remain future work.

Strategies must use `update_execution_plan(context, services, updated_plan)` when changing a
running plan. That operation updates the Run snapshot and emits `plan.updated`, `step.started`,
`step.waiting`, `step.resumed`, `step.completed`, `step.failed`, or `step.cancelled` events. Plans
are serialized into waiting checkpoints, so human input resumes the same Run and Step.

Applications may replace only Plan behavior while retaining the normal non-Plan strategy:

```python
runtime = AgentRuntime(planning_strategy=PlanningStrategy(summarize=False))
```

Applications that require a fixed supplied Plan may explicitly use
`PlanningStrategy(replan_after_step=False)`.

If an application supplies a custom primary `strategy`, the Runtime does not silently override it
for Plans unless `planning_strategy` is also supplied.

See [ReAct on the Shared Model/Tool Loop](REACT.md) for standalone ReAct, Plan Step selection,
ActionBatch semantics, and WAITING/resume behavior.

Execution plans describe orchestration state; Skills still carry reusable instructions and domain
workflows, while Tools perform atomic effects. A BI or lineage application therefore owns its
Skills and Tools and may supply a planning strategy, without those domain concepts entering the
base package.
