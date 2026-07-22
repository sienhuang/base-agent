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

Pass an initial plan through `Agent.run(..., plan=plan)` or create one inside a strategy. Strategies
must use `update_execution_plan(context, services, updated_plan)` when changing a running plan.
That operation updates the Run snapshot and emits generic `plan.updated`, `step.started`,
`step.completed`, or `step.failed` events. Plans are also serialized into waiting checkpoints, so
human input does not break planner state.

Execution plans describe orchestration state; Skills still carry reusable instructions and domain
workflows, while Tools perform atomic effects. A BI or lineage application therefore owns its
Skills and Tools and may supply a planning strategy, without those domain concepts entering the
base package.
