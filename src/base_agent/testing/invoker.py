"""Deterministic AgentInvoker test double for Flow strategy tests."""

from collections import deque
from collections.abc import Iterable, Mapping
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from base_agent.flows import (
    AgentInvocationCancel,
    AgentInvocationRequest,
    AgentInvocationResult,
    AgentInvocationResume,
)
from base_agent.models import (
    AgentResultStatus,
    Artifact,
    PendingInput,
    TokenUsage,
)


class ScriptedAgentInvokerExhaustedError(RuntimeError):
    """Raised when a Flow invokes an Agent without another scripted outcome."""


class ScriptedAgentOutcome(BaseModel):
    """Identification-free result payload consumed by ScriptedAgentInvoker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: AgentResultStatus
    output: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    artifacts: tuple[Artifact, ...] = ()
    pending_input: PendingInput | None = None
    error: str | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ScriptedAgentInvoker:
    """Return per-Agent outcomes in order and retain immutable call evidence."""

    def __init__(
        self,
        outcomes: Mapping[str, Iterable[ScriptedAgentOutcome]],
    ) -> None:
        self._outcomes = {
            agent_key: deque(agent_outcomes)
            for agent_key, agent_outcomes in outcomes.items()
        }
        self._requests: list[AgentInvocationRequest] = []
        self._resumes: list[AgentInvocationResume] = []
        self._cancellations: list[AgentInvocationCancel] = []
        self._waiting: dict[UUID, AgentInvocationRequest] = {}
        self._cancelled: set[UUID] = set()

    @property
    def requests(self) -> tuple[AgentInvocationRequest, ...]:
        return tuple(self._requests)

    @property
    def resumes(self) -> tuple[AgentInvocationResume, ...]:
        return tuple(self._resumes)

    @property
    def cancellations(self) -> tuple[AgentInvocationCancel, ...]:
        return tuple(self._cancellations)

    async def invoke(self, request: AgentInvocationRequest) -> AgentInvocationResult:
        self._requests.append(request)
        return self._result(request, self._next(request.agent_key))

    async def resume(self, request: AgentInvocationResume) -> AgentInvocationResult:
        self._resumes.append(request)
        try:
            original = self._waiting[request.invocation_id]
        except KeyError as exc:
            raise ValueError(
                f"invocation '{request.invocation_id}' is not waiting"
            ) from exc
        if original.flow_run_id != request.flow_run_id:
            raise ValueError("resume flow_run_id does not match the waiting invocation")
        if original.agent_key != request.agent_key:
            raise ValueError("resume agent_key does not match the waiting invocation")
        result = self._result(original, self._next(original.agent_key))
        if result.status is not AgentResultStatus.WAITING:
            del self._waiting[request.invocation_id]
        return result

    async def cancel(self, request: AgentInvocationCancel) -> None:
        self._cancellations.append(request)
        if request.invocation_id in self._cancelled:
            return
        original = self._waiting.pop(request.invocation_id, None)
        if original is not None:
            if original.flow_run_id != request.flow_run_id:
                raise ValueError(
                    "cancel flow_run_id does not match the waiting invocation"
                )
            if original.agent_key != request.agent_key:
                raise ValueError(
                    "cancel agent_key does not match the waiting invocation"
                )
        self._cancelled.add(request.invocation_id)

    def _next(self, agent_key: str) -> ScriptedAgentOutcome:
        outcomes = self._outcomes.get(agent_key)
        if not outcomes:
            raise ScriptedAgentInvokerExhaustedError(
                f"no scripted outcome remains for Agent '{agent_key}'"
            )
        return outcomes.popleft()

    def _result(
        self,
        request: AgentInvocationRequest,
        outcome: ScriptedAgentOutcome,
    ) -> AgentInvocationResult:
        result = AgentInvocationResult(
            flow_run_id=request.flow_run_id,
            invocation_id=request.invocation_id,
            agent_key=request.agent_key,
            **outcome.model_dump(mode="python"),
        )
        if result.status is AgentResultStatus.WAITING:
            self._waiting[result.invocation_id] = request
        return result
