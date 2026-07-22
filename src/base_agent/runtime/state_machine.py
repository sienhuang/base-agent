"""Explicit state transitions for one runtime execution."""

from base_agent.models import RunStatus

ExecutionState = RunStatus


class InvalidStateTransitionError(RuntimeError):
    """Raised when runtime code attempts a transition not allowed by the contract."""


_ALLOWED_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    ExecutionState.CREATED: frozenset(
        {ExecutionState.RUNNING, ExecutionState.CANCELLED, ExecutionState.FAILED}
    ),
    ExecutionState.RUNNING: frozenset(
        {
            ExecutionState.COMPLETED,
            ExecutionState.FAILED,
            ExecutionState.CANCELLED,
            ExecutionState.LIMIT_REACHED,
            ExecutionState.WAITING,
        }
    ),
    ExecutionState.COMPLETED: frozenset(),
    ExecutionState.FAILED: frozenset(),
    ExecutionState.CANCELLED: frozenset(),
    ExecutionState.LIMIT_REACHED: frozenset(),
    ExecutionState.WAITING: frozenset(
        {ExecutionState.RUNNING, ExecutionState.CANCELLED, ExecutionState.FAILED}
    ),
}


class RuntimeStateMachine:
    """Small mutable state holder with validated terminal transitions."""

    def __init__(self, initial: ExecutionState = ExecutionState.CREATED) -> None:
        self._state = initial

    @property
    def state(self) -> ExecutionState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return not _ALLOWED_TRANSITIONS[self._state]

    def transition_to(self, target: ExecutionState) -> None:
        if target not in _ALLOWED_TRANSITIONS[self._state]:
            raise InvalidStateTransitionError(
                f"invalid runtime transition: {self._state.value} -> {target.value}"
            )
        self._state = target
