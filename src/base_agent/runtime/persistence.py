"""Shared persistence operations for runtime and orchestration strategies."""

from base_agent.models import RunStatus
from base_agent.models.run import utc_now
from base_agent.runtime.context import RuntimeContext
from base_agent.stores import RunStore


async def save_context_snapshot(context: RuntimeContext, run_store: RunStore) -> None:
    """Persist the current mutable runtime context into the immutable Run aggregate."""

    existing = await run_store.get(context.run_id)
    updated = existing.model_copy(
        update={
            "status": RunStatus(context.state_machine.state),
            "step_count": context.step_count,
            "tool_call_count": context.tool_call_count,
            "usage": context.usage,
            "output": context.output,
            "error": context.error,
            "attachments": context.attachments,
            "artifacts": tuple(context.artifacts),
            "metadata": {
                **existing.metadata,
                "pending_input": (
                    context.pending_input.model_dump(mode="json")
                    if context.pending_input is not None
                    else None
                ),
                "plan": (
                    context.plan.model_dump(mode="json") if context.plan is not None else None
                ),
                "resource_failures": [
                    failure.model_dump(mode="json")
                    for failure in context.resource_failures
                ],
                "memory": {
                    "initialized": context.memory_initialized,
                    "error": context.memory_error,
                    "matches": [
                        {"id": str(match.record.id), "score": match.score}
                        for match in context.memories
                    ],
                },
            },
            "updated_at": utc_now(),
        },
        deep=True,
    )
    await run_store.save(updated)
