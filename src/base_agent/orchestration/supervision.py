"""Shared handling for strategy-independent supervision decisions."""

from base_agent.models import EventType, Message
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.state_machine import ExecutionState, InvalidStateTransitionError
from base_agent.stores import EventStore
from base_agent.supervision import SupervisionAction, SupervisionDecision


async def apply_supervision_decision(
    context: RuntimeContext,
    decision: SupervisionDecision,
    event_store: EventStore,
    *,
    append_message: bool = True,
) -> None:
    await event_store.emit(
        context.run_id,
        EventType.SUPERVISOR_INTERVENED,
        decision.model_dump(mode="json"),
    )
    if append_message and decision.message:
        context.messages.append(Message.system(decision.message))
    if decision.action is SupervisionAction.STOP:
        if decision.terminal_status is None:
            raise InvalidStateTransitionError("stop decision has no terminal status")
        context.error = decision.reason
        context.state_machine.transition_to(ExecutionState(decision.terminal_status))
