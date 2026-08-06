"""Immutable lifecycle state for one top-level Flow Run."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.flows.models import (
    AgentInvocationInput,
    AgentInvocationRequest,
    AgentInvocationResult,
    FlowBudget,
    FlowDefinition,
)
from base_agent.models import AgentResultStatus, RunStatus, TokenUsage
from base_agent.models.run import utc_now

_RESULT_TO_RUN_STATUS = {
    AgentResultStatus.COMPLETED: RunStatus.COMPLETED,
    AgentResultStatus.FAILED: RunStatus.FAILED,
    AgentResultStatus.CANCELLED: RunStatus.CANCELLED,
    AgentResultStatus.INTERRUPTED: RunStatus.INTERRUPTED,
    AgentResultStatus.LIMIT_REACHED: RunStatus.LIMIT_REACHED,
    AgentResultStatus.WAITING: RunStatus.WAITING,
}
_RUN_TO_RESULT_STATUS = {
    run_status: result_status
    for result_status, run_status in _RESULT_TO_RUN_STATUS.items()
}
_TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
    RunStatus.LIMIT_REACHED,
}
_ERROR_STATUSES = _TERMINAL_STATUSES - {RunStatus.COMPLETED}


class InvalidFlowTransitionError(RuntimeError):
    """Raised when a Flow or AgentInvocation violates lifecycle ordering."""


class AgentInvocation(BaseModel):
    """One Agent execution record owned by a top-level Flow Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    flow_run_id: UUID
    sequence: int = Field(ge=1)
    agent_key: str
    definition_id: str
    definition_version: str
    definition_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    input: AgentInvocationInput
    status: RunStatus = RunStatus.RUNNING
    result: AgentInvocationResult | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_result(self) -> AgentInvocation:
        if self.status is RunStatus.CREATED:
            if self.result is not None:
                raise ValueError("a created AgentInvocation cannot include a result")
            return self
        if self.status is RunStatus.RUNNING:
            if (
                self.result is not None
                and self.result.status is not AgentResultStatus.WAITING
            ):
                raise ValueError(
                    "a resumed AgentInvocation may only retain its waiting result"
                )
            if self.result is not None:
                self._validate_result_identity(self.result)
            return self
        if self.result is None:
            raise ValueError("a settled AgentInvocation requires a result")
        self._validate_result_identity(self.result)
        expected = _RESULT_TO_RUN_STATUS[self.result.status]
        if self.status is not expected:
            raise ValueError("AgentInvocation status does not match its result")
        return self

    @property
    def usage(self) -> TokenUsage:
        return self.result.usage if self.result is not None else TokenUsage()

    def settle(self, result: AgentInvocationResult) -> AgentInvocation:
        """Replace a running or waiting segment with its latest cumulative result."""
        if self.status not in {RunStatus.RUNNING, RunStatus.WAITING}:
            raise InvalidFlowTransitionError(
                f"cannot settle AgentInvocation in state '{self.status.value}'"
            )
        if result.flow_run_id != self.flow_run_id:
            raise ValueError("AgentInvocation result flow_run_id does not match")
        if result.invocation_id != self.id:
            raise ValueError("AgentInvocation result invocation_id does not match")
        if result.agent_key != self.agent_key:
            raise ValueError("AgentInvocation result agent_key does not match")
        return self.model_copy(
            update={
                "status": _RESULT_TO_RUN_STATUS[result.status],
                "result": result,
                "updated_at": utc_now(),
            }
        )

    def resume(self) -> AgentInvocation:
        """Claim a waiting invocation before calling the external resume transport."""
        if self.status is not RunStatus.WAITING:
            raise InvalidFlowTransitionError(
                f"cannot resume AgentInvocation in state '{self.status.value}'"
            )
        return self.model_copy(
            update={"status": RunStatus.RUNNING, "updated_at": utc_now()}
        )

    def terminate(self, status: RunStatus, error: str) -> AgentInvocation:
        """Terminate an active invocation because its owning Flow has terminated."""
        if status not in _ERROR_STATUSES:
            raise ValueError("AgentInvocation termination requires an error status")
        if not error.strip():
            raise ValueError("AgentInvocation termination requires an error")
        if self.status not in {
            RunStatus.CREATED,
            RunStatus.RUNNING,
            RunStatus.WAITING,
        }:
            raise InvalidFlowTransitionError(
                f"cannot terminate AgentInvocation in state '{self.status.value}'"
            )
        result = AgentInvocationResult(
            flow_run_id=self.flow_run_id,
            invocation_id=self.id,
            agent_key=self.agent_key,
            status=_RUN_TO_RESULT_STATUS[status],
            usage=self.usage,
            artifacts=self.result.artifacts if self.result is not None else (),
            error=error,
        )
        return self.model_copy(
            update={"status": status, "result": result, "updated_at": utc_now()}
        )

    def _validate_result_identity(self, result: AgentInvocationResult) -> None:
        if result.flow_run_id != self.flow_run_id:
            raise ValueError("AgentInvocation result flow_run_id does not match")
        if result.invocation_id != self.id:
            raise ValueError("AgentInvocation result invocation_id does not match")
        if result.agent_key != self.agent_key:
            raise ValueError("AgentInvocation result agent_key does not match")


class FlowRunState(BaseModel):
    """Serializable orchestration state attached to one future top-level Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID = Field(default_factory=uuid4)
    definition_id: str
    definition_version: str
    definition_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    budget: FlowBudget
    deadline_at: datetime | None = None
    revision: int = Field(default=1, ge=1)
    status: RunStatus = RunStatus.CREATED
    invocations: tuple[AgentInvocation, ...] = ()
    active_invocation_id: UUID | None = None
    output: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_state(self) -> FlowRunState:
        ids = [invocation.id for invocation in self.invocations]
        if len(set(ids)) != len(ids):
            raise ValueError("Flow AgentInvocation ids must be unique")
        sequences = [invocation.sequence for invocation in self.invocations]
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("Flow AgentInvocation sequences must be contiguous")
        if len(self.invocations) > self.max_invocations:
            raise ValueError("Flow invocation count exceeds max_invocations")
        if (self.budget.timeout_seconds is None) != (self.deadline_at is None):
            raise ValueError("Flow deadline must match its timeout budget")
        if any(invocation.flow_run_id != self.run_id for invocation in self.invocations):
            raise ValueError("AgentInvocation belongs to another Flow Run")

        active = self._active_or_none()
        if self.active_invocation_id is not None and active is None:
            raise ValueError("active_invocation_id does not identify an invocation")
        if self.status is RunStatus.CREATED and self.invocations:
            raise ValueError("a created Flow Run cannot contain invocations")
        if self.status is RunStatus.WAITING:
            if active is None or active.status is not RunStatus.WAITING:
                raise ValueError("a waiting Flow Run requires one waiting invocation")
        if self.status is RunStatus.RUNNING and active is not None:
            if active.status is not RunStatus.RUNNING:
                raise ValueError("an active running Flow requires a running invocation")
        if self.status in _TERMINAL_STATUSES and active is not None:
            raise ValueError("a terminal Flow Run cannot retain an active invocation")
        if self.status is RunStatus.COMPLETED:
            if self.output is None:
                raise ValueError("a completed Flow Run requires output")
            if self.error is not None:
                raise ValueError("a completed Flow Run cannot include an error")
        elif self.status in _ERROR_STATUSES and not self.error:
            raise ValueError(
                f"a Flow Run with status '{self.status.value}' requires an error"
            )
        return self

    @classmethod
    def create(
        cls,
        definition: FlowDefinition,
        *,
        run_id: UUID | None = None,
        now: datetime | None = None,
    ) -> FlowRunState:
        created_at = now or utc_now()
        deadline_at = (
            created_at + timedelta(seconds=definition.budget.timeout_seconds)
            if definition.budget.timeout_seconds is not None
            else None
        )
        return cls(
            run_id=run_id or uuid4(),
            definition_id=definition.id,
            definition_version=definition.version,
            definition_fingerprint=definition.fingerprint,
            budget=definition.budget,
            deadline_at=deadline_at,
            created_at=created_at,
            updated_at=created_at,
        )

    @property
    def max_invocations(self) -> int:
        return self.budget.max_invocations

    @property
    def usage(self) -> TokenUsage:
        total = TokenUsage()
        for invocation in self.invocations:
            total += invocation.usage
        return total

    @property
    def active_invocation(self) -> AgentInvocation | None:
        return self._active_or_none()

    @property
    def model_call_count(self) -> int:
        return sum(
            _result_count(invocation.result, "model_calls")
            for invocation in self.invocations
        )

    @property
    def tool_call_count(self) -> int:
        return sum(
            _result_count(invocation.result, "tool_calls")
            for invocation in self.invocations
        )

    def start(self) -> FlowRunState:
        if self.status is not RunStatus.CREATED:
            raise InvalidFlowTransitionError(
                f"cannot start Flow Run in state '{self.status.value}'"
            )
        return self._evolve(status=RunStatus.RUNNING)

    def begin_invocation(
        self,
        request: AgentInvocationRequest,
        *,
        definition: FlowDefinition,
    ) -> FlowRunState:
        if self.status is not RunStatus.RUNNING:
            raise InvalidFlowTransitionError(
                f"cannot invoke an Agent while Flow Run is '{self.status.value}'"
            )
        if self.active_invocation_id is not None:
            raise InvalidFlowTransitionError(
                "Flow Run already has an active AgentInvocation"
            )
        self._require_definition(definition)
        if request.flow_run_id != self.run_id:
            raise ValueError("AgentInvocation request belongs to another Flow Run")
        expected_sequence = len(self.invocations) + 1
        if request.sequence != expected_sequence:
            raise ValueError(
                f"AgentInvocation sequence must be {expected_sequence}, "
                f"got {request.sequence}"
            )
        if expected_sequence > self.max_invocations:
            raise InvalidFlowTransitionError(
                f"Flow Run invocation limit reached ({self.max_invocations})"
            )
        agent_definition = definition.agent(request.agent_key)
        if (
            request.definition_id != agent_definition.id
            or request.definition_version != agent_definition.version
            or request.definition_fingerprint != agent_definition.fingerprint
        ):
            raise ValueError(
                "AgentInvocation request definition does not match FlowDefinition"
            )
        invocation = AgentInvocation(
            id=request.invocation_id,
            flow_run_id=self.run_id,
            sequence=request.sequence,
            agent_key=request.agent_key,
            definition_id=request.definition_id,
            definition_version=request.definition_version,
            definition_fingerprint=request.definition_fingerprint,
            input=request.input,
        )
        return self._evolve(
            invocations=(*self.invocations, invocation),
            active_invocation_id=invocation.id,
        )

    def settle_invocation(
        self,
        result: AgentInvocationResult,
    ) -> FlowRunState:
        if self.status not in {RunStatus.RUNNING, RunStatus.WAITING}:
            raise InvalidFlowTransitionError(
                f"cannot settle an invocation while Flow Run is '{self.status.value}'"
            )
        active = self.active_invocation
        if active is None:
            raise InvalidFlowTransitionError("Flow Run has no active AgentInvocation")
        settled = active.settle(result)
        invocations = tuple(
            settled if invocation.id == settled.id else invocation
            for invocation in self.invocations
        )
        waiting = settled.status is RunStatus.WAITING
        return self._evolve(
            status=RunStatus.WAITING if waiting else RunStatus.RUNNING,
            invocations=invocations,
            active_invocation_id=settled.id if waiting else None,
        )

    def resume_invocation(self, invocation_id: UUID) -> FlowRunState:
        """Claim the active waiting invocation before invoking resume transport."""
        if self.status is not RunStatus.WAITING:
            raise InvalidFlowTransitionError(
                f"cannot resume an invocation while Flow Run is '{self.status.value}'"
            )
        active = self.active_invocation
        if active is None:
            raise InvalidFlowTransitionError("Flow Run has no active AgentInvocation")
        if active.id != invocation_id:
            raise ValueError("invocation_id does not match the waiting invocation")
        resumed = active.resume()
        invocations = tuple(
            resumed if invocation.id == resumed.id else invocation
            for invocation in self.invocations
        )
        return self._evolve(
            status=RunStatus.RUNNING,
            invocations=invocations,
        )

    def complete(self, output: str) -> FlowRunState:
        if self.status is not RunStatus.RUNNING:
            raise InvalidFlowTransitionError(
                f"cannot complete Flow Run in state '{self.status.value}'"
            )
        if self.active_invocation_id is not None:
            raise InvalidFlowTransitionError(
                "cannot complete Flow Run with an active AgentInvocation"
            )
        if not self.invocations:
            raise InvalidFlowTransitionError(
                "cannot complete Flow Run without an AgentInvocation"
            )
        if any(
            invocation.status is not RunStatus.COMPLETED
            for invocation in self.invocations
        ):
            raise InvalidFlowTransitionError(
                "cannot complete Flow Run with an unsuccessful AgentInvocation"
            )
        return self._evolve(
            status=RunStatus.COMPLETED,
            output=output,
            error=None,
        )

    def fail(self, error: str) -> FlowRunState:
        return self._terminate(RunStatus.FAILED, error)

    def cancel(self, error: str = "Flow Run cancellation requested") -> FlowRunState:
        return self._terminate(RunStatus.CANCELLED, error)

    def interrupt(self, error: str = "Flow Run interrupted") -> FlowRunState:
        return self._terminate(RunStatus.INTERRUPTED, error)

    def reach_limit(self, error: str = "Flow Run invocation limit reached") -> FlowRunState:
        return self._terminate(RunStatus.LIMIT_REACHED, error)

    def _terminate(self, status: RunStatus, error: str) -> FlowRunState:
        if self.status in _TERMINAL_STATUSES:
            raise InvalidFlowTransitionError(
                f"cannot terminate Flow Run in state '{self.status.value}'"
            )
        if not error.strip():
            raise ValueError("Flow Run termination requires an error")
        active = self.active_invocation
        invocations = self.invocations
        if active is not None:
            terminated = active.terminate(status, error)
            invocations = tuple(
                terminated if invocation.id == active.id else invocation
                for invocation in self.invocations
            )
        return self._evolve(
            status=status,
            invocations=invocations,
            active_invocation_id=None,
            error=error,
        )

    def _evolve(self, **updates: Any) -> FlowRunState:
        """Return the next aggregate revision after one lifecycle transition."""
        return self.model_copy(
            update={
                **updates,
                "revision": self.revision + 1,
                "updated_at": utc_now(),
            }
        )

    def _require_definition(self, definition: FlowDefinition) -> None:
        if (
            definition.id != self.definition_id
            or definition.version != self.definition_version
            or definition.fingerprint != self.definition_fingerprint
        ):
            raise ValueError("FlowDefinition does not match this Flow Run")

    def _active_or_none(self) -> AgentInvocation | None:
        if self.active_invocation_id is None:
            return None
        return next(
            (
                invocation
                for invocation in self.invocations
                if invocation.id == self.active_invocation_id
            ),
            None,
        )


def _result_count(
    result: AgentInvocationResult | None,
    key: str,
) -> int:
    if result is None:
        return 0
    value = result.metadata.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
