"""Process-local retention for background Run handles."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, cast, runtime_checkable

from base_agent.agent import Agent
from base_agent.execution import (
    ExecutionAttempt,
    ExecutionStore,
    RecoveryDecision,
    RecoveryDecisionRecord,
    RecoveryEvidenceSnapshot,
    RecoveryStrategy,
)
from base_agent.flow_store import FlowRunRecord, FlowRunStore
from base_agent.flows import FlowRunHandle
from base_agent.governance import ToolConfirmation
from base_agent.run_handle import RunHandle
from base_agent.run_store import AgentRunRecord, InMemoryRunStore, RunStore
from base_agent.server.executor import LocalRunExecutor
from base_agent.server.queued_flow_executor import (
    DurableFlowDefinition,
    QueuedFlowExecutor,
)


class RunNotFoundError(LookupError):
    """The requested process-local Run is unknown."""


class FlowNotRegisteredError(LookupError):
    """The requested Flow strategy is not registered by the application."""


class FlowRunNotFoundError(LookupError):
    """The requested process-local Flow Run is unknown."""


class RegisteredFlow(Protocol):
    """A Flow strategy that can launch a process-local background execution."""

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
    ) -> FlowRunHandle[Any]: ...


class RegisteredRunExecutor(Protocol):
    async def startup(self) -> tuple[AgentRunRecord, ...]: ...

    async def close(self) -> None: ...

    async def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        idempotency_key: str | None = None,
    ) -> AgentRunRecord: ...

    async def get(self, run_id: str) -> AgentRunRecord: ...

    def local_handle(self, run_id: str) -> RunHandle | None: ...

    async def resume(self, run_id: str, user_input: str) -> AgentRunRecord: ...

    async def confirm(
        self,
        run_id: str,
        confirmation: ToolConfirmation,
    ) -> AgentRunRecord: ...

    async def cancel(self, run_id: str) -> AgentRunRecord: ...



@runtime_checkable
class ResumableRegisteredFlow(Protocol):
    async def resume(self, flow_run_id: str, user_input: str) -> Any: ...

    async def confirm(self, flow_run_id: str, confirmation: ToolConfirmation) -> Any: ...


class InMemoryRunRegistry:
    """Retain local handles while delegating lifecycle authority to a RunStore."""

    def __init__(
        self,
        agent: Agent,
        run_store: RunStore | None = None,
        *,
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        runtime_build_id: str | None = None,
        executor: RegisteredRunExecutor | None = None,
    ) -> None:
        self.executor = executor or LocalRunExecutor(
            agent,
            run_store or InMemoryRunStore(),
            lease_seconds=lease_seconds,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
            runtime_build_id=runtime_build_id,
        )

    async def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        idempotency_key: str | None = None,
    ) -> AgentRunRecord:
        return await self.executor.start(
            prompt,
            stream=stream,
            idempotency_key=idempotency_key,
        )

    def get(self, run_id: str) -> RunHandle:
        handle = self.executor.local_handle(run_id)
        if handle is None:
            raise RunNotFoundError(f"Run '{run_id}' was not found")
        return handle

    async def record(self, run_id: str) -> AgentRunRecord:
        return await self.executor.get(run_id)

    async def resume(self, run_id: str, user_input: str) -> AgentRunRecord:
        return await self.executor.resume(run_id, user_input)

    async def confirm(
        self,
        run_id: str,
        confirmation: ToolConfirmation,
    ) -> AgentRunRecord:
        return await self.executor.confirm(run_id, confirmation)

    async def cancel(self, run_id: str) -> AgentRunRecord:
        return await self.executor.cancel(run_id)

    async def latest_attempt(self, run_id: str) -> ExecutionAttempt | None:
        method = getattr(self.executor, "latest_attempt", None)
        if method is None:
            return None
        return cast(ExecutionAttempt | None, await method(run_id))

    async def recovery_evidence(self, run_id: str) -> RecoveryEvidenceSnapshot | None:
        method = getattr(self.executor, "recovery_evidence", None)
        if method is None:
            return None
        return cast(RecoveryEvidenceSnapshot, await method(run_id))

    async def decide_recovery(
        self,
        run_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[AgentRunRecord, RecoveryDecisionRecord]:
        method = getattr(self.executor, "decide_recovery", None)
        if method is None:
            raise RunNotFoundError("Operator recovery requires queued execution mode")
        return cast(
            tuple[AgentRunRecord, RecoveryDecisionRecord],
            await method(
                run_id,
                decision=decision,
                strategy=strategy,
                subject_id=subject_id,
                reason_code=reason_code,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            ),
        )


class InMemoryFlowRegistry:
    """Register Flows and select local handles or durable queued execution."""

    def __init__(
        self,
        flows: Mapping[str, RegisteredFlow],
        *,
        flow_store: FlowRunStore | None = None,
        execution_store: ExecutionStore | None = None,
        runtime_route: str = "default",
        runtime_build_id: str | None = None,
    ) -> None:
        if any(not name.strip() for name in flows):
            raise ValueError("Flow registration names must not be blank")
        self._flows = dict(flows)
        self._handles: dict[str, FlowRunHandle[Any]] = {}
        self._owners: dict[str, RegisteredFlow] = {}
        self._continued_results: dict[str, Any] = {}
        self.queued_executor = (
            QueuedFlowExecutor(
                {
                    name: _durable_flow(name, flow)
                    for name, flow in self._flows.items()
                },
                flow_store,
                execution_store,
                runtime_route=runtime_route,
                runtime_build_id=runtime_build_id or "development",
            )
            if flow_store is not None and execution_store is not None
            else None
        )

    @property
    def queued(self) -> bool:
        return self.queued_executor is not None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._flows)

    def start(
        self,
        name: str,
        prompt: str,
        *,
        stream: bool = False,
    ) -> FlowRunHandle[Any]:
        try:
            flow = self._flows[name]
        except KeyError as exc:
            raise FlowNotRegisteredError(f"Flow '{name}' is not registered") from exc
        handle = flow.start(prompt, stream=stream)
        self._handles[handle.flow_run_id] = handle
        self._owners[handle.flow_run_id] = flow
        return handle

    async def enqueue(
        self,
        name: str,
        prompt: str,
        *,
        stream: bool = False,
        idempotency_key: str | None = None,
    ) -> FlowRunRecord:
        executor = self.queued_executor
        if executor is None:
            raise RuntimeError("Flow Registry is not configured for queued execution")
        return await executor.start(
            name,
            prompt,
            stream=stream,
            idempotency_key=idempotency_key,
        )

    async def record(self, flow_run_id: str) -> FlowRunRecord:
        executor = self.queued_executor
        if executor is None:
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' has no durable control record"
            )
        return await executor.get(flow_run_id)

    def get(self, flow_run_id: str) -> FlowRunHandle[Any]:
        try:
            return self._handles[flow_run_id]
        except KeyError as exc:
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' was not found"
            ) from exc

    def continued_result(self, flow_run_id: str) -> Any | None:
        return self._continued_results.get(flow_run_id)

    async def resume(self, flow_run_id: str, user_input: str) -> Any:
        if self.queued_executor is not None:
            return await self.queued_executor.resume(flow_run_id, user_input)
        flow = self._owner(flow_run_id)
        if not isinstance(flow, ResumableRegisteredFlow):
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' does not support WAITING continuation"
            )
        result = await flow.resume(flow_run_id, user_input)
        self._continued_results[flow_run_id] = result
        return result

    async def confirm(self, flow_run_id: str, confirmation: ToolConfirmation) -> Any:
        if self.queued_executor is not None:
            return await self.queued_executor.confirm(flow_run_id, confirmation)
        flow = self._owner(flow_run_id)
        if not isinstance(flow, ResumableRegisteredFlow):
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' does not support WAITING continuation"
            )
        result = await flow.confirm(flow_run_id, confirmation)
        self._continued_results[flow_run_id] = result
        return result

    async def cancel(self, flow_run_id: str) -> FlowRunRecord:
        if self.queued_executor is None:
            raise FlowRunNotFoundError("durable cancellation requires queued execution")
        return await self.queued_executor.cancel(flow_run_id)

    async def latest_attempt(self, flow_run_id: str) -> ExecutionAttempt | None:
        if self.queued_executor is None:
            return None
        return await self.queued_executor.latest_attempt(flow_run_id)

    async def recovery_evidence(
        self,
        flow_run_id: str,
    ) -> RecoveryEvidenceSnapshot | None:
        if self.queued_executor is None:
            return None
        return await self.queued_executor.recovery_evidence(flow_run_id)

    async def decide_recovery(
        self,
        flow_run_id: str,
        *,
        decision: RecoveryDecision,
        strategy: RecoveryStrategy | None,
        subject_id: str,
        reason_code: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> tuple[FlowRunRecord, RecoveryDecisionRecord]:
        if self.queued_executor is None:
            raise FlowRunNotFoundError("Flow recovery requires queued execution")
        return await self.queued_executor.decide_recovery(
            flow_run_id,
            decision=decision,
            strategy=strategy,
            subject_id=subject_id,
            reason_code=reason_code,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    def _owner(self, flow_run_id: str) -> RegisteredFlow:
        try:
            return self._owners[flow_run_id]
        except KeyError as exc:
            raise FlowRunNotFoundError(
                f"Flow Run '{flow_run_id}' is not owned by this server process"
            ) from exc


def _durable_flow(name: str, flow: RegisteredFlow) -> DurableFlowDefinition:
    if not isinstance(flow, DurableFlowDefinition):
        raise ValueError(
            f"queued Flow '{name}' must expose id, kind, and definition_fingerprint"
        )
    return flow
