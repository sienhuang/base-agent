"""Host-owned Planner/ReAct orchestration over ordinary Agent Runs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.agent import Agent
from base_agent.events import EventEmitter, EventStore, InMemoryEventStore
from base_agent.flows.handle import FlowRunHandle
from base_agent.flows.models import FlowStatus
from base_agent.memory import MemoryScope
from base_agent.models import Event, RunResult, RunStatus
from base_agent.observability import Observer
from base_agent.run_handle import RunHandle, RunNotCancellableError

_UNFORWARDED_CHILD_EVENT_KINDS = frozenset({"model.tool_call.delta"})


class PlannerAction(StrEnum):
    CREATE = "create"
    CONTINUE = "continue"
    REVISE = "revise"
    COMPLETE = "complete"
    FAIL = "fail"


class PlanStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PlanStepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class ChildRunRole(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class PlanStepDraft(_StrictFrozenModel):
    """One proposed Step. The Host assigns its durable identity."""

    description: str = Field(min_length=1)


class PlannerDecision(_StrictFrozenModel):
    """Structured Planner output used for both creation and review."""

    action: PlannerAction
    goal: str | None
    steps: tuple[PlanStepDraft, ...]
    reason: str
    final_output: str | None

    @model_validator(mode="after")
    def validate_action_payload(self) -> PlannerDecision:
        if self.action is PlannerAction.CREATE:
            if not self.goal:
                raise ValueError("create requires a non-blank goal")
            if not self.steps:
                raise ValueError("create requires at least one Step")
        elif self.action is PlannerAction.REVISE:
            if not self.steps:
                raise ValueError("revise requires at least one replacement Step")
        elif self.action is PlannerAction.COMPLETE:
            if not self.final_output:
                raise ValueError("complete requires a non-blank final_output")
        elif self.action is PlannerAction.FAIL and not self.reason:
            raise ValueError("fail requires a non-blank reason")

        if self.action not in {PlannerAction.CREATE, PlannerAction.REVISE} and self.steps:
            raise ValueError(f"{self.action.value} must not include replacement Steps")
        if self.action is not PlannerAction.COMPLETE and self.final_output is not None:
            raise ValueError(f"{self.action.value} must not include final_output")
        return self


class StepOutcome(_StrictFrozenModel):
    """Business outcome returned by an Executor after one ReAct Run."""

    success: bool
    summary: str = Field(min_length=1)
    output: str | None
    error: str | None

    @model_validator(mode="after")
    def validate_failure(self) -> StepOutcome:
        if not self.success and not self.error:
            raise ValueError("an unsuccessful Step requires a non-blank error")
        if self.success and self.error is not None:
            raise ValueError("a successful Step must not include an error")
        return self


class PlanStep(_StrictFrozenModel):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)
    status: PlanStepStatus = PlanStepStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)
    outcome: StepOutcome | None = None


class Plan(_StrictFrozenModel):
    id: str = Field(min_length=1)
    revision: int = Field(ge=1)
    goal: str = Field(min_length=1)
    status: PlanStatus = PlanStatus.RUNNING
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True, slots=True)
class PlanningLimits:
    max_steps: int = 12
    max_revisions: int = 4
    max_child_runs: int = 25

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_revisions < 1:
            raise ValueError("max_revisions must be at least 1")
        if self.max_child_runs < 3:
            raise ValueError("max_child_runs must be at least 3")


@dataclass(frozen=True, slots=True)
class FlowChildRun:
    """Explicit correlation from a Flow phase to one ordinary Agent Run."""

    role: ChildRunRole
    phase: Literal["create", "review", "execute"]
    flow_run_id: str
    agent_run_id: str
    plan_id: str
    source_plan_revision: int | None
    target_plan_revision: int | None
    step_id: str | None
    attempt: int | None
    run: RunResult


@dataclass(frozen=True, slots=True)
class PlannerReActResult:
    flow_run_id: str
    plan_id: str
    status: FlowStatus
    output: str
    plan: Plan | None
    child_runs: tuple[FlowChildRun, ...]
    events: tuple[Event, ...]
    error: str | None = None


@dataclass(slots=True)
class _ActiveChild:
    role: ChildRunRole
    phase: Literal["create", "review", "execute"]
    handle: RunHandle
    flow_run_id: str
    plan_id: str
    source_plan_revision: int | None
    target_plan_revision: int | None = None
    step_id: str | None = None
    attempt: int | None = None

    def finish(self, run: RunResult) -> FlowChildRun:
        return FlowChildRun(
            role=self.role,
            phase=self.phase,
            flow_run_id=self.flow_run_id,
            agent_run_id=self.handle.run_id,
            plan_id=self.plan_id,
            source_plan_revision=self.source_plan_revision,
            target_plan_revision=self.target_plan_revision,
            step_id=self.step_id,
            attempt=self.attempt,
            run=run,
        )


@dataclass(slots=True)
class _FlowRunState:
    flow_run_id: str
    plan_id: str
    prompt: str
    emitter: EventEmitter
    planner_memory_scope: MemoryScope | None
    executor_memory_scope: MemoryScope | None
    user_message_id: str
    stream: bool
    plan: Plan | None = None
    output: str = ""
    error: str | None = None
    child_runs: list[FlowChildRun] = field(default_factory=list)
    active_child: _ActiveChild | None = None
    cancelled_child: _ActiveChild | None = None


class _FlowExecutionError(RuntimeError):
    pass


class PlannerReActFlow:
    """Create, execute, and review a Host-owned Plan using fixed Agent roles."""

    def __init__(
        self,
        *,
        id: str,
        planner: Agent,
        executor: Agent,
        limits: PlanningLimits | None = None,
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        forward_child_events: bool = False,
    ) -> None:
        if not id.strip():
            raise ValueError("PlannerReActFlow id must not be blank")
        planner_response_model = planner.definition.response_model
        if planner_response_model is None or not issubclass(
            planner_response_model,
            PlannerDecision,
        ):
            raise ValueError("Planner Agent response_model must be PlannerDecision")
        executor_response_model = executor.definition.response_model
        if executor_response_model is None or not issubclass(
            executor_response_model,
            StepOutcome,
        ):
            raise ValueError("Executor Agent response_model must be StepOutcome")
        self.id = id
        self.planner = planner
        self.executor = executor
        self.limits = limits if limits is not None else PlanningLimits()
        self.event_store = event_store if event_store is not None else InMemoryEventStore()
        self.forward_child_events = forward_child_events
        self._observers = tuple(observers)

    async def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        planner_memory_scope: MemoryScope | None = None,
        executor_memory_scope: MemoryScope | None = None,
    ) -> PlannerReActResult:
        """Run to completion; cancellation from the caller propagates after child cleanup."""
        state = self._new_state(
            prompt,
            on_event=on_event,
            planner_memory_scope=planner_memory_scope,
            executor_memory_scope=executor_memory_scope,
            stream=stream,
        )
        return await self._execute(state)

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        planner_memory_scope: MemoryScope | None = None,
        executor_memory_scope: MemoryScope | None = None,
    ) -> FlowRunHandle[PlannerReActResult]:
        """Start one process-local Flow and expose Events and cancellation."""
        state = self._new_state(
            prompt,
            on_event=on_event,
            planner_memory_scope=planner_memory_scope,
            executor_memory_scope=executor_memory_scope,
            stream=stream,
        )

        async def operation() -> PlannerReActResult:
            return await self._execute(state)

        async def cancel_operation() -> PlannerReActResult:
            return await self._cancelled_result(state)

        return FlowRunHandle._launch(
            flow_run_id=state.flow_run_id,
            operation=operation,
            cancel_operation=cancel_operation,
            event_store=self.event_store,
        )

    def _new_state(
        self,
        prompt: str,
        *,
        on_event: Observer | None,
        planner_memory_scope: MemoryScope | None,
        executor_memory_scope: MemoryScope | None,
        stream: bool,
    ) -> _FlowRunState:
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        flow_run_id = str(uuid4())
        observers = self._observers if on_event is None else (*self._observers, on_event)
        return _FlowRunState(
            flow_run_id=flow_run_id,
            plan_id=str(uuid4()),
            prompt=prompt,
            emitter=EventEmitter(flow_run_id, self.event_store, observers),
            planner_memory_scope=planner_memory_scope,
            executor_memory_scope=executor_memory_scope,
            user_message_id=str(uuid4()),
            stream=stream,
        )

    async def _execute(self, state: _FlowRunState) -> PlannerReActResult:
        try:
            await state.emitter.emit(
                "flow.started",
                flow_id=self.id,
                plan_id=state.plan_id,
                planner_agent_id=self.planner.definition.id,
                executor_agent_id=self.executor.definition.id,
                stream=state.stream,
            )
            await state.emitter.emit(
                "message.created",
                message_id=state.user_message_id,
                role="user",
                content=state.prompt,
                event_scope="flow",
            )
            decision, planner_run_id = await self._invoke_planner(
                state,
                phase="create",
                prompt=_create_planner_prompt(state.prompt, self.executor),
                source_revision=None,
            )
            if decision.action is PlannerAction.FAIL:
                raise _FlowExecutionError(decision.reason)
            if decision.action is not PlannerAction.CREATE:
                raise _FlowExecutionError(
                    f"initial Planner action must be 'create', got '{decision.action.value}'"
                )

            state.plan = _create_plan(state.plan_id, decision, self.limits)
            self._set_last_planner_target(state, state.plan.revision)
            await state.emitter.emit(
                "plan.created",
                plan_id=state.plan.id,
                plan_revision=state.plan.revision,
                planner_run_id=planner_run_id,
                plan=state.plan.model_dump(mode="json"),
            )

            while True:
                step_index = _next_pending_step_index(state.plan)
                if step_index is None:
                    raise _FlowExecutionError(
                        "Plan has no pending Step but Planner has not completed the Flow"
                    )
                executed_count = sum(
                    child.role is ChildRunRole.EXECUTOR for child in state.child_runs
                )
                if executed_count >= self.limits.max_steps:
                    raise _FlowExecutionError(
                        f"Flow exceeded max_steps={self.limits.max_steps}"
                    )

                outcome, executor_run_id = await self._execute_step(
                    state,
                    step_index=step_index,
                )
                decision, planner_run_id = await self._invoke_planner(
                    state,
                    phase="review",
                    prompt=_review_planner_prompt(
                        state.prompt,
                        _require_plan(state),
                        step_index,
                        outcome,
                    ),
                    source_revision=_require_plan(state).revision,
                )
                _validate_review_goal(_require_plan(state), decision)
                await state.emitter.emit(
                    "plan.reviewed",
                    plan_id=state.plan_id,
                    plan_revision=_require_plan(state).revision,
                    step_id=_require_plan(state).steps[step_index].id,
                    executor_run_id=executor_run_id,
                    planner_run_id=planner_run_id,
                    action=decision.action.value,
                    reason=decision.reason,
                )

                if decision.action is PlannerAction.CONTINUE:
                    if _next_pending_step_index(_require_plan(state)) is None:
                        raise _FlowExecutionError(
                            "Planner returned 'continue' but the Plan has no pending Step"
                        )
                    self._set_last_planner_target(state, _require_plan(state).revision)
                    continue
                if decision.action is PlannerAction.REVISE:
                    source_revision = _require_plan(state).revision
                    state.plan = _revise_plan(_require_plan(state), decision, self.limits)
                    self._set_last_planner_target(state, state.plan.revision)
                    await state.emitter.emit(
                        "plan.revised",
                        plan_id=state.plan.id,
                        source_plan_revision=source_revision,
                        target_plan_revision=state.plan.revision,
                        planner_run_id=planner_run_id,
                        plan=state.plan.model_dump(mode="json"),
                    )
                    continue
                if decision.action is PlannerAction.COMPLETE:
                    self._set_last_planner_target(state, _require_plan(state).revision)
                    state.plan = _complete_plan(_require_plan(state))
                    state.output = decision.final_output or ""
                    await state.emitter.emit(
                        "plan.completed",
                        plan_id=state.plan.id,
                        plan_revision=state.plan.revision,
                        planner_run_id=planner_run_id,
                    )
                    await state.emitter.emit(
                        "message.completed",
                        plan_id=state.plan.id,
                        plan_revision=state.plan.revision,
                        message_id=str(uuid4()),
                        role="assistant",
                        content=state.output,
                        tool_call_count=0,
                        event_scope="flow",
                    )
                    await state.emitter.emit(
                        "flow.succeeded",
                        plan_id=state.plan.id,
                        plan_revision=state.plan.revision,
                    )
                    return await self._result(state, FlowStatus.SUCCEEDED)
                if decision.action is PlannerAction.FAIL:
                    self._set_last_planner_target(state, _require_plan(state).revision)
                    raise _FlowExecutionError(decision.reason)
                raise _FlowExecutionError(
                    f"review Planner action cannot be '{decision.action.value}'"
                )
        except asyncio.CancelledError:
            await self._cancel_active_child(state)
            raise
        except _FlowExecutionError as exc:
            return await self._failed_result(state, str(exc))
        except Exception as exc:
            return await self._failed_result(
                state,
                f"{type(exc).__name__}: {exc}",
            )

    async def _invoke_planner(
        self,
        state: _FlowRunState,
        *,
        phase: Literal["create", "review"],
        prompt: str,
        source_revision: int | None,
        target_revision: int | None = None,
    ) -> tuple[PlannerDecision, str]:
        active = self._start_child(
            state,
            agent=self.planner,
            role=ChildRunRole.PLANNER,
            phase=phase,
            prompt=prompt,
            memory_scope=state.planner_memory_scope,
            source_revision=source_revision,
            target_revision=target_revision,
        )
        await state.emitter.emit(
            "planner.started",
            phase=phase,
            plan_id=state.plan_id,
            source_plan_revision=source_revision,
            target_plan_revision=target_revision,
            agent_run_id=active.handle.run_id,
        )
        run = await self._wait_for_active_child(state)
        if run.status is not RunStatus.SUCCEEDED:
            raise _FlowExecutionError(
                f"Planner Run '{run.run_id}' ended with status '{run.status.value}': "
                f"{run.error or 'no error detail'}"
            )
        if not isinstance(run.output, PlannerDecision):
            raise _FlowExecutionError(
                f"Planner Run '{run.run_id}' did not return PlannerDecision"
            )
        await state.emitter.emit(
            "planner.completed",
            phase=phase,
            plan_id=state.plan_id,
            source_plan_revision=source_revision,
            agent_run_id=run.run_id,
            action=run.output.action.value,
        )
        return run.output, run.run_id

    async def _execute_step(
        self,
        state: _FlowRunState,
        *,
        step_index: int,
    ) -> tuple[StepOutcome, str]:
        plan = _require_plan(state)
        step = plan.steps[step_index]
        attempt = step.attempt_count + 1
        active = self._start_child(
            state,
            agent=self.executor,
            role=ChildRunRole.EXECUTOR,
            phase="execute",
            prompt=_executor_prompt(state.prompt, plan, step_index),
            memory_scope=state.executor_memory_scope,
            source_revision=plan.revision,
            step_id=step.id,
            attempt=attempt,
        )
        state.plan = _replace_step(
            plan,
            step_index,
            step.model_copy(
                update={
                    "status": PlanStepStatus.RUNNING,
                    "attempt_count": attempt,
                }
            ),
        )
        await state.emitter.emit(
            "step.started",
            plan_id=plan.id,
            plan_revision=plan.revision,
            step_id=step.id,
            attempt=attempt,
            agent_run_id=active.handle.run_id,
        )
        run = await self._wait_for_active_child(state)
        if run.status is not RunStatus.SUCCEEDED:
            state.plan = _replace_step_status(
                _require_plan(state),
                step_index,
                PlanStepStatus.FAILED,
            )
            await state.emitter.emit(
                "step.failed",
                plan_id=plan.id,
                plan_revision=plan.revision,
                step_id=step.id,
                attempt=attempt,
                agent_run_id=run.run_id,
                error=run.error,
                failure_kind="agent_run",
            )
            raise _FlowExecutionError(
                f"Executor Run '{run.run_id}' failed for Step '{step.id}': "
                f"{run.error or run.status.value}"
            )
        if not isinstance(run.output, StepOutcome):
            raise _FlowExecutionError(
                f"Executor Run '{run.run_id}' did not return StepOutcome"
            )

        outcome = run.output
        terminal_status = (
            PlanStepStatus.SUCCEEDED if outcome.success else PlanStepStatus.FAILED
        )
        current_step = _require_plan(state).steps[step_index]
        state.plan = _replace_step(
            _require_plan(state),
            step_index,
            current_step.model_copy(
                update={
                    "status": terminal_status,
                    "outcome": outcome,
                }
            ),
        )
        await state.emitter.emit(
            "step.completed" if outcome.success else "step.failed",
            plan_id=plan.id,
            plan_revision=plan.revision,
            step_id=step.id,
            attempt=attempt,
            agent_run_id=run.run_id,
            success=outcome.success,
            summary=outcome.summary,
            error=outcome.error,
            failure_kind=None if outcome.success else "business_outcome",
        )
        return outcome, run.run_id

    def _start_child(
        self,
        state: _FlowRunState,
        *,
        agent: Agent,
        role: ChildRunRole,
        phase: Literal["create", "review", "execute"],
        prompt: str,
        memory_scope: MemoryScope | None,
        source_revision: int | None,
        target_revision: int | None = None,
        step_id: str | None = None,
        attempt: int | None = None,
    ) -> _ActiveChild:
        if state.active_child is not None:
            raise RuntimeError("Flow already has an active child Run")
        if len(state.child_runs) >= self.limits.max_child_runs:
            raise _FlowExecutionError(
                f"Flow exceeded max_child_runs={self.limits.max_child_runs}"
            )
        async def forward_child_event(event: Event) -> None:
            if event.kind in _UNFORWARDED_CHILD_EVENT_KINDS:
                return
            payload = dict(event.payload)
            payload.update(
                {
                    "event_scope": "child_run",
                    "flow_run_id": state.flow_run_id,
                    "plan_id": state.plan_id,
                    "agent_run_id": event.run_id,
                    "child_role": role.value,
                    "flow_phase": phase,
                    "source_plan_revision": source_revision,
                    "target_plan_revision": target_revision,
                    "step_id": step_id,
                    "attempt": attempt,
                    "child_sequence": event.sequence,
                    "child_kind": event.kind,
                }
            )
            await state.emitter.emit(
                _forwarded_child_kind(event.kind),
                turn=event.turn,
                **payload,
            )

        handle = agent.start(
            prompt,
            stream=state.stream,
            on_event=(forward_child_event if self.forward_child_events else None),
            memory_scope=memory_scope,
        )
        active = _ActiveChild(
            role=role,
            phase=phase,
            handle=handle,
            flow_run_id=state.flow_run_id,
            plan_id=state.plan_id,
            source_plan_revision=source_revision,
            target_plan_revision=target_revision,
            step_id=step_id,
            attempt=attempt,
        )
        state.active_child = active
        return active

    async def _wait_for_active_child(self, state: _FlowRunState) -> RunResult:
        active = state.active_child
        if active is None:
            raise RuntimeError("Flow has no active child Run")
        run = await active.handle.wait()
        state.child_runs.append(active.finish(run))
        state.active_child = None
        return run

    async def _cancel_active_child(self, state: _FlowRunState) -> None:
        active = state.active_child
        if active is None:
            return
        try:
            run = await active.handle.cancel()
        except RunNotCancellableError:
            run = await active.handle.wait()
        state.child_runs.append(active.finish(run))
        state.active_child = None
        state.cancelled_child = active
        if active.role is ChildRunRole.EXECUTOR and state.plan is not None:
            step_index = _step_index(state.plan, active.step_id)
            if step_index is not None:
                state.plan = _replace_step_status(
                    state.plan,
                    step_index,
                    PlanStepStatus.CANCELLED,
                )

    def _set_last_planner_target(self, state: _FlowRunState, revision: int) -> None:
        if not state.child_runs or state.child_runs[-1].role is not ChildRunRole.PLANNER:
            raise RuntimeError("latest child Run is not a Planner")
        state.child_runs[-1] = replace(
            state.child_runs[-1],
            target_plan_revision=revision,
        )

    async def _failed_result(
        self,
        state: _FlowRunState,
        error: str,
    ) -> PlannerReActResult:
        state.error = error
        if state.plan is not None:
            state.plan = state.plan.model_copy(update={"status": PlanStatus.FAILED})
            await state.emitter.emit(
                "plan.failed",
                plan_id=state.plan.id,
                plan_revision=state.plan.revision,
                error=error,
            )
        await state.emitter.emit(
            "flow.failed",
            plan_id=state.plan_id,
            error=error,
        )
        return await self._result(state, FlowStatus.FAILED)

    async def _cancelled_result(self, state: _FlowRunState) -> PlannerReActResult:
        if state.plan is not None:
            state.plan = state.plan.model_copy(update={"status": PlanStatus.CANCELLED})
        active = state.cancelled_child
        if active is not None:
            await state.emitter.emit(
                "step.cancelled" if active.role is ChildRunRole.EXECUTOR else "planner.cancelled",
                phase=active.phase,
                plan_id=active.plan_id,
                plan_revision=active.source_plan_revision,
                step_id=active.step_id,
                attempt=active.attempt,
                agent_run_id=active.handle.run_id,
            )
        if state.plan is not None:
            await state.emitter.emit(
                "plan.cancelled",
                plan_id=state.plan.id,
                plan_revision=state.plan.revision,
            )
        state.error = "flow cancellation requested"
        await state.emitter.emit(
            "flow.cancelled",
            plan_id=state.plan_id,
            reason=state.error,
        )
        return await self._result(state, FlowStatus.CANCELLED)

    async def _result(
        self,
        state: _FlowRunState,
        status: FlowStatus,
    ) -> PlannerReActResult:
        return PlannerReActResult(
            flow_run_id=state.flow_run_id,
            plan_id=state.plan_id,
            status=status,
            output=state.output,
            plan=state.plan,
            child_runs=tuple(state.child_runs),
            events=await state.emitter.events(),
            error=state.error,
        )


def _create_plan(
    plan_id: str,
    decision: PlannerDecision,
    limits: PlanningLimits,
) -> Plan:
    if len(decision.steps) > limits.max_steps:
        raise _FlowExecutionError(
            f"initial Plan has {len(decision.steps)} Steps, exceeding max_steps={limits.max_steps}"
        )
    return Plan(
        id=plan_id,
        revision=1,
        goal=decision.goal or "",
        steps=tuple(
            PlanStep(id=str(uuid4()), description=draft.description)
            for draft in decision.steps
        ),
    )


def _revise_plan(
    plan: Plan,
    decision: PlannerDecision,
    limits: PlanningLimits,
) -> Plan:
    target_revision = plan.revision + 1
    if target_revision > limits.max_revisions:
        raise _FlowExecutionError(
            f"Plan exceeded max_revisions={limits.max_revisions}"
        )
    first_pending = _next_pending_step_index(plan)
    prefix = plan.steps if first_pending is None else plan.steps[:first_pending]
    replacement = tuple(
        PlanStep(id=str(uuid4()), description=draft.description)
        for draft in decision.steps
    )
    if len(prefix) + len(replacement) > limits.max_steps:
        raise _FlowExecutionError(
            "revised Plan would exceed "
            f"max_steps={limits.max_steps} including its immutable terminal prefix"
        )
    return plan.model_copy(
        update={
            "revision": target_revision,
            "steps": (*prefix, *replacement),
        }
    )


def _complete_plan(plan: Plan) -> Plan:
    steps = tuple(
        step.model_copy(update={"status": PlanStepStatus.SKIPPED})
        if step.status is PlanStepStatus.PENDING
        else step
        for step in plan.steps
    )
    return plan.model_copy(
        update={
            "status": PlanStatus.SUCCEEDED,
            "steps": steps,
        }
    )


def _replace_step(plan: Plan, index: int, step: PlanStep) -> Plan:
    steps = list(plan.steps)
    steps[index] = step
    return plan.model_copy(update={"steps": tuple(steps)})


def _replace_step_status(plan: Plan, index: int, status: PlanStepStatus) -> Plan:
    return _replace_step(
        plan,
        index,
        plan.steps[index].model_copy(update={"status": status}),
    )


def _next_pending_step_index(plan: Plan) -> int | None:
    return next(
        (
            index
            for index, step in enumerate(plan.steps)
            if step.status is PlanStepStatus.PENDING
        ),
        None,
    )


def _step_index(plan: Plan, step_id: str | None) -> int | None:
    if step_id is None:
        return None
    return next(
        (index for index, step in enumerate(plan.steps) if step.id == step_id),
        None,
    )


def _require_plan(state: _FlowRunState) -> Plan:
    if state.plan is None:
        raise RuntimeError("Flow has no Plan")
    return state.plan


def _validate_review_goal(plan: Plan, decision: PlannerDecision) -> None:
    if decision.goal is not None and decision.goal != plan.goal:
        raise _FlowExecutionError(
            "review Planner attempted to change the Plan goal from "
            f"{plan.goal!r} to {decision.goal!r}"
        )


def _forwarded_child_kind(kind: str) -> str:
    if kind.startswith(("run.", "message.")):
        return f"child.{kind}"
    return kind


def _create_planner_prompt(user_prompt: str, executor: Agent) -> str:
    executor_capabilities = {
        "agent_id": executor.definition.id,
        "description": executor.definition.description,
        "tools": executor.definition.tools,
        "skills": executor.definition.skills,
        "permissions": sorted(executor.definition.permissions),
    }
    return (
        "Create a concise executable Plan for the user request. Return action='create'. "
        "Do not execute the task. The Host assigns Step ids and owns Plan state. "
        "Return every schema field: set final_output=null; reason should briefly explain "
        "the decomposition.\n\n"
        f"User request:\n{user_prompt}\n\n"
        "Fixed Executor capabilities:\n"
        f"{json.dumps(executor_capabilities, ensure_ascii=False, indent=2)}"
    )


def _executor_prompt(user_prompt: str, plan: Plan, step_index: int) -> str:
    step = plan.steps[step_index]
    previous = [
        {
            "id": item.id,
            "description": item.description,
            "status": item.status.value,
            "summary": item.outcome.summary if item.outcome is not None else None,
            "error": item.outcome.error if item.outcome is not None else None,
        }
        for item in plan.steps[:step_index]
    ]
    return (
        "Execute only the current Plan Step using your configured Tools and Skills. "
        "Return every StepOutcome field; use error=null on success and a non-blank error "
        "on failure. output is a string or null. Do not return a new Plan.\n\n"
        f"Original user request:\n{user_prompt}\n\n"
        f"Plan id: {plan.id}\nPlan revision: {plan.revision}\nPlan goal: {plan.goal}\n\n"
        "Previous terminal Steps:\n"
        f"{json.dumps(previous, ensure_ascii=False, indent=2)}\n\n"
        f"Current Step id: {step.id}\nCurrent Step:\n{step.description}"
    )


def _review_planner_prompt(
    user_prompt: str,
    plan: Plan,
    step_index: int,
    outcome: StepOutcome,
) -> str:
    step = plan.steps[step_index]
    return (
        "Review the latest Step outcome against the original request and current Plan. "
        "Return exactly one action: continue to retain the remaining suffix, revise to "
        "replace only the remaining pending suffix, complete with final_output, or fail. "
        "Never rewrite terminal Steps or change the Plan goal. Return every schema field: "
        "outside create, goal should be null; an exact copy of the current Plan goal is "
        "also accepted, but any changed value will fail. Use steps=[] outside revise and "
        "final_output=null outside complete.\n\n"
        f"Original user request:\n{user_prompt}\n\n"
        "Current Plan:\n"
        f"{plan.model_dump_json(indent=2)}\n\n"
        f"Latest Step id: {step.id}\nLatest outcome:\n{outcome.model_dump_json(indent=2)}"
    )
