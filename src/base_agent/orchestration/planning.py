"""Plan lifecycle operations shared by planning strategies."""

from base_agent.models import EventType, ExecutionPlan, StepStatus
from base_agent.orchestration.protocol import RuntimeServices
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.persistence import save_context_snapshot


async def update_execution_plan(
    context: RuntimeContext,
    services: RuntimeServices,
    plan: ExecutionPlan,
) -> None:
    """Replace a context plan, persist it, and emit generic lifecycle events."""

    previous = context.plan
    if previous is not None:
        if previous.id != plan.id:
            raise ValueError("a running context cannot replace its plan identity")
        if plan.revision <= previous.revision:
            raise ValueError("an updated plan must have a newer revision")
    context.plan = plan
    await save_context_snapshot(context, services.run_store)
    await services.event_store.emit(
        context.run_id,
        EventType.PLAN_CREATED if previous is None else EventType.PLAN_UPDATED,
        {"plan": plan.model_dump(mode="json")},
    )
    if previous is None:
        return

    previous_steps = {step.id: step for step in previous.steps}
    event_types = {
        StepStatus.RUNNING: EventType.STEP_STARTED,
        StepStatus.COMPLETED: EventType.STEP_COMPLETED,
        StepStatus.FAILED: EventType.STEP_FAILED,
    }
    for step in plan.steps:
        old_step = previous_steps.get(step.id)
        if old_step is None or old_step.status is step.status:
            continue
        event_type = event_types.get(step.status)
        if event_type is not None:
            await services.event_store.emit(
                context.run_id,
                event_type,
                {"plan_id": plan.id, "step": step.model_dump(mode="json")},
            )
