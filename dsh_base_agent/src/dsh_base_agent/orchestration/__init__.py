"""Standalone business Flow definitions over DSH-backed Agent Runs."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dsh_base_agent.events import EventStore, InMemoryEventStore
from dsh_base_agent.memory import MemoryScope
from dsh_base_agent.models import Event, Observer, RunInvocation, RunResult, RunStatus


class _Agent(Protocol):
    definition: Any

    async def run(
        self,
        prompt: str,
        *,
        memory_scope: MemoryScope | None = None,
        invocation: RunInvocation | None = None,
    ) -> RunResult: ...


class FlowStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 1

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


type StageHandoff = Callable[[RunResult], str]
type StageVerifier = Callable[[RunResult], bool | Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    agent: _Agent
    prompt: str = ""
    input_from: str | None = None
    handoff: StageHandoff | None = None
    verify: StageVerifier | None = None
    retry: RetryPolicy = RetryPolicy()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("stage name must not be blank")
        if self.input_from is not None and not self.input_from.strip():
            raise ValueError("stage input_from must not be blank")


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    run: RunResult
    handoff: str
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class FlowResult:
    flow_run_id: str
    status: FlowStatus
    output: str
    stages: tuple[StageResult, ...]
    events: tuple[Event, ...]
    stage_attempts: tuple[StageResult, ...] = ()
    error: str | None = None


class _HasFlowStatus(Protocol):
    @property
    def status(self) -> FlowStatus: ...


class FlowRunNotCancellableError(RuntimeError):
    pass


class FlowRunHandle[FlowResultT: _HasFlowStatus]:
    def __init__(
        self,
        flow_run_id: str,
        task: asyncio.Task[FlowResultT],
        event_store: EventStore,
    ) -> None:
        self.flow_run_id = flow_run_id
        self._task = task
        self._events = event_store
        self._settled: FlowResultT | None = None

    @classmethod
    def launch(
        cls,
        flow_run_id: str,
        operation: Callable[[], Coroutine[Any, Any, FlowResultT]],
        event_store: EventStore,
    ) -> FlowRunHandle[FlowResultT]:
        return cls(
            flow_run_id,
            asyncio.create_task(operation(), name=f"dsh-flow-{flow_run_id}"),
            event_store,
        )

    @property
    def status(self) -> FlowStatus:
        if self._settled is not None:
            return self._settled.status
        if not self._task.done():
            return FlowStatus.RUNNING
        if self._task.cancelled():
            return FlowStatus.CANCELLED
        try:
            return self._task.result().status
        except Exception:
            return FlowStatus.FAILED

    async def wait(self) -> FlowResultT:
        if self._settled is None:
            self._settled = await asyncio.shield(self._task)
        return self._settled

    async def cancel(self) -> FlowResultT:
        if self._task.done():
            result = await self.wait()
            if result.status is not FlowStatus.CANCELLED:
                raise FlowRunNotCancellableError("Flow already completed")
            return result
        self._task.cancel("flow cancellation requested")
        return await self.wait()

    async def events(self, *, after_sequence: int = 0) -> tuple[Event, ...]:
        return await self._events.list(self.flow_run_id, after_sequence=after_sequence)

    def stream(self, *, after_sequence: int = 0) -> AsyncIterator[Event]:
        return self._events.subscribe(
            self.flow_run_id,
            after_sequence=after_sequence,
        )


class SequentialFlow:
    def __init__(
        self,
        *,
        id: str,
        stages: Sequence[Stage],
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        **unsupported: object,
    ) -> None:
        del unsupported
        if not id.strip() or not stages:
            raise ValueError("Flow id must not be blank and stages must not be empty")
        names = [stage.name for stage in stages]
        if len(set(names)) != len(names):
            raise ValueError("Flow Stage names must be unique")
        known = {"user"}
        for stage in stages:
            if stage.input_from is not None and stage.input_from not in known:
                raise ValueError(f"Stage '{stage.name}' references unavailable input")
            known.add(stage.name)
        self.id = id
        self.stages = tuple(stages)
        self.event_store = event_store or InMemoryEventStore()
        self._observers = tuple(observers)

    async def run(
        self,
        prompt: str,
        *,
        on_event: Observer | None = None,
        flow_run_id: str | None = None,
        **options: object,
    ) -> FlowResult:
        del options
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        run_id = flow_run_id or str(uuid4())
        observers = self._observers + ((on_event,) if on_event else ())
        completed: list[StageResult] = []
        handoffs = {"user": prompt}
        await _emit(self.event_store, run_id, "flow.started", {"flow_id": self.id}, observers)
        try:
            for stage in self.stages:
                source = stage.input_from or (completed[-1].name if completed else "user")
                stage_input = handoffs[source]
                stage_prompt = (
                    f"{stage.prompt}\n\n{stage_input}".strip() if stage.prompt else stage_input
                )
                accepted: StageResult | None = None
                for attempt in range(1, stage.retry.max_attempts + 1):
                    await _emit(
                        self.event_store,
                        run_id,
                        "stage.started",
                        {"stage": stage.name, "attempt": attempt},
                        observers,
                    )
                    child = await stage.agent.run(
                        stage_prompt,
                        invocation=RunInvocation(
                            flow_run_id=run_id,
                            invocation_id=str(uuid4()),
                            stage=stage.name,
                            attempt=attempt,
                        ),
                    )
                    if child.status is not RunStatus.SUCCEEDED:
                        raise RuntimeError(child.error or f"Stage '{stage.name}' failed")
                    verified = True if stage.verify is None else stage.verify(child)
                    if inspect.isawaitable(verified):
                        verified = await verified
                    result = StageResult(
                        stage.name,
                        child,
                        stage.handoff(child) if stage.handoff else child.text,
                        attempt,
                    )
                    if verified:
                        accepted = result
                        break
                if accepted is None:
                    raise RuntimeError(f"Stage '{stage.name}' failed verification")
                completed.append(accepted)
                handoffs[stage.name] = accepted.handoff
                await _emit(
                    self.event_store,
                    run_id,
                    "stage.succeeded",
                    {"stage": stage.name, "agent_run_id": accepted.run.run_id},
                    observers,
                )
            output = completed[-1].handoff
            await _emit(self.event_store, run_id, "flow.succeeded", {"output": output}, observers)
            return FlowResult(
                run_id,
                FlowStatus.SUCCEEDED,
                output,
                tuple(completed),
                await self.event_store.list(run_id),
                tuple(completed),
            )
        except asyncio.CancelledError:
            await _emit(self.event_store, run_id, "flow.cancelled", {}, observers)
            return FlowResult(
                run_id,
                FlowStatus.CANCELLED,
                "",
                tuple(completed),
                await self.event_store.list(run_id),
                tuple(completed),
                "flow cancellation requested",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await _emit(self.event_store, run_id, "flow.failed", {"error": error}, observers)
            return FlowResult(
                run_id,
                FlowStatus.FAILED,
                "",
                tuple(completed),
                await self.event_store.list(run_id),
                tuple(completed),
                error,
            )

    def start(
        self, prompt: str, *, flow_run_id: str | None = None, **options: Any
    ) -> FlowRunHandle[FlowResult]:
        run_id = flow_run_id or str(uuid4())
        return FlowRunHandle.launch(
            run_id,
            lambda: self.run(prompt, flow_run_id=run_id, **options),
            self.event_store,
        )


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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class PlanStepDraft(_StrictModel):
    description: str = Field(min_length=1)


class PlannerDecision(_StrictModel):
    action: PlannerAction
    goal: str | None
    steps: tuple[PlanStepDraft, ...]
    reason: str
    final_output: str | None

    @model_validator(mode="after")
    def validate_payload(self) -> PlannerDecision:
        if self.action is PlannerAction.CREATE and (not self.goal or not self.steps):
            raise ValueError("create requires a goal and steps")
        if self.action is PlannerAction.REVISE and not self.steps:
            raise ValueError("revise requires replacement steps")
        if self.action is PlannerAction.COMPLETE and not self.final_output:
            raise ValueError("complete requires final_output")
        return self


class StepOutcome(_StrictModel):
    success: bool
    summary: str = Field(min_length=1)
    output: str | None
    error: str | None
    artifact_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_outcome(self) -> StepOutcome:
        if self.success and self.error is not None:
            raise ValueError("a successful Step must not include an error")
        if not self.success and not self.error:
            raise ValueError("an unsuccessful Step requires an error")
        return self


class PlanStep(_StrictModel):
    id: str
    description: str
    status: PlanStepStatus = PlanStepStatus.PENDING
    attempt_count: int = 0
    outcome: StepOutcome | None = None


class Plan(_StrictModel):
    id: str
    revision: int
    goal: str
    status: PlanStatus = PlanStatus.RUNNING
    steps: tuple[PlanStep, ...]


@dataclass(frozen=True, slots=True)
class PlanningLimits:
    max_steps: int = 12
    max_revisions: int = 4
    max_child_runs: int = 25

    def __post_init__(self) -> None:
        if min(self.max_steps, self.max_revisions, self.max_child_runs) < 1:
            raise ValueError("planning limits must be positive")


class ChildRunRole(StrEnum):
    PLANNER = "planner"
    EXECUTOR = "executor"


@dataclass(frozen=True, slots=True)
class FlowChildRun:
    role: ChildRunRole
    phase: Literal["create", "review", "execute"]
    flow_run_id: str
    agent_run_id: str
    plan_id: str
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


class PlannerReActFlow:
    def __init__(
        self,
        *,
        id: str,
        planner: _Agent,
        executor: _Agent,
        limits: PlanningLimits | None = None,
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        **unsupported: object,
    ) -> None:
        del unsupported
        if not id.strip():
            raise ValueError("PlannerReActFlow id must not be blank")
        planner_model = planner.definition.response_model
        if planner_model is None or not issubclass(planner_model, PlannerDecision):
            raise ValueError("Planner Agent response_model must be PlannerDecision")
        executor_model = executor.definition.response_model
        if executor_model is None or not issubclass(executor_model, StepOutcome):
            raise ValueError("Executor Agent response_model must be StepOutcome")
        self.id = id
        self.planner = planner
        self.executor = executor
        self.limits = limits or PlanningLimits()
        self.event_store = event_store or InMemoryEventStore()
        self._observers = tuple(observers)

    async def run(
        self,
        prompt: str,
        *,
        on_event: Observer | None = None,
        flow_run_id: str | None = None,
        planner_memory_scope: MemoryScope | None = None,
        executor_memory_scope: MemoryScope | None = None,
        **options: object,
    ) -> PlannerReActResult:
        del options
        run_id, plan_id = flow_run_id or str(uuid4()), str(uuid4())
        observers = self._observers + ((on_event,) if on_event else ())
        children: list[FlowChildRun] = []
        plan: Plan | None = None
        await _emit(self.event_store, run_id, "flow.started", {"flow_id": self.id}, observers)
        try:
            decision = await self._planner_call(
                prompt,
                run_id,
                plan_id,
                "create",
                children,
                planner_memory_scope,
            )
            if decision.action is not PlannerAction.CREATE:
                raise RuntimeError("initial Planner action must be 'create'")
            plan = Plan(
                id=plan_id,
                revision=1,
                goal=decision.goal or "",
                steps=tuple(
                    PlanStep(id=f"step-{index + 1}", description=item.description)
                    for index, item in enumerate(decision.steps)
                ),
            )
            await _emit(
                self.event_store,
                run_id,
                "plan.created",
                {"plan": plan.model_dump(mode="json")},
                observers,
            )
            executed = 0
            step_index = 0
            while True:
                if len(children) >= self.limits.max_child_runs:
                    raise RuntimeError("Flow exceeded max_child_runs")
                if step_index >= len(plan.steps):
                    raise RuntimeError("Plan has no remaining Step")
                if executed >= self.limits.max_steps:
                    raise RuntimeError("Flow exceeded max_steps")
                step = plan.steps[step_index]
                executor_run = await self.executor.run(
                    _executor_prompt(prompt, plan, step),
                    memory_scope=executor_memory_scope,
                    invocation=RunInvocation(run_id, str(uuid4()), step_id=step.id, attempt=1),
                )
                if executor_run.status is not RunStatus.SUCCEEDED or not isinstance(
                    executor_run.output, StepOutcome
                ):
                    raise RuntimeError(executor_run.error or "Executor did not return StepOutcome")
                children.append(
                    FlowChildRun(
                        ChildRunRole.EXECUTOR,
                        "execute",
                        run_id,
                        executor_run.run_id,
                        plan_id,
                        step.id,
                        1,
                        executor_run,
                    )
                )
                executed += 1
                updated_step = step.model_copy(
                    update={
                        "status": PlanStepStatus.SUCCEEDED
                        if executor_run.output.success
                        else PlanStepStatus.FAILED,
                        "attempt_count": 1,
                        "outcome": executor_run.output,
                    }
                )
                plan = plan.model_copy(
                    update={
                        "steps": plan.steps[:step_index]
                        + (updated_step,)
                        + plan.steps[step_index + 1 :]
                    }
                )
                decision = await self._planner_call(
                    _review_prompt(prompt, plan, updated_step),
                    run_id,
                    plan_id,
                    "review",
                    children,
                    planner_memory_scope,
                )
                if decision.action is PlannerAction.COMPLETE:
                    output = decision.final_output or ""
                    plan = plan.model_copy(update={"status": PlanStatus.SUCCEEDED})
                    await _emit(
                        self.event_store, run_id, "flow.succeeded", {"output": output}, observers
                    )
                    return PlannerReActResult(
                        run_id,
                        plan_id,
                        FlowStatus.SUCCEEDED,
                        output,
                        plan,
                        tuple(children),
                        await self.event_store.list(run_id),
                    )
                if decision.action is PlannerAction.FAIL:
                    raise RuntimeError(decision.reason)
                if decision.action is PlannerAction.REVISE:
                    if plan.revision >= self.limits.max_revisions:
                        raise RuntimeError("Flow exceeded max_revisions")
                    plan = Plan(
                        id=plan.id,
                        revision=plan.revision + 1,
                        goal=decision.goal or plan.goal,
                        steps=tuple(
                            PlanStep(
                                id=f"step-{plan.revision + 1}-{index + 1}",
                                description=item.description,
                            )
                            for index, item in enumerate(decision.steps)
                        ),
                    )
                    step_index = 0
                elif decision.action is PlannerAction.CONTINUE:
                    step_index += 1
                else:
                    raise RuntimeError(f"unsupported review action '{decision.action.value}'")
        except asyncio.CancelledError:
            await _emit(self.event_store, run_id, "flow.cancelled", {}, observers)
            return PlannerReActResult(
                run_id,
                plan_id,
                FlowStatus.CANCELLED,
                "",
                plan,
                tuple(children),
                await self.event_store.list(run_id),
                "flow cancellation requested",
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await _emit(self.event_store, run_id, "flow.failed", {"error": error}, observers)
            return PlannerReActResult(
                run_id,
                plan_id,
                FlowStatus.FAILED,
                "",
                plan,
                tuple(children),
                await self.event_store.list(run_id),
                error,
            )

    async def _planner_call(
        self,
        prompt: str,
        flow_run_id: str,
        plan_id: str,
        phase: Literal["create", "review"],
        children: list[FlowChildRun],
        memory_scope: MemoryScope | None,
    ) -> PlannerDecision:
        result = await self.planner.run(
            prompt,
            memory_scope=memory_scope,
            invocation=RunInvocation(flow_run_id, str(uuid4())),
        )
        children.append(
            FlowChildRun(
                ChildRunRole.PLANNER,
                phase,
                flow_run_id,
                result.run_id,
                plan_id,
                None,
                None,
                result,
            )
        )
        if result.status is not RunStatus.SUCCEEDED or not isinstance(
            result.output, PlannerDecision
        ):
            raise RuntimeError(result.error or "Planner did not return PlannerDecision")
        return result.output

    def start(
        self, prompt: str, *, flow_run_id: str | None = None, **options: Any
    ) -> FlowRunHandle[PlannerReActResult]:
        run_id = flow_run_id or str(uuid4())
        return FlowRunHandle.launch(
            run_id,
            lambda: self.run(prompt, flow_run_id=run_id, **options),
            self.event_store,
        )


class Flow:
    @staticmethod
    def sequence(**options: Any) -> SequentialFlow:
        return SequentialFlow(**options)

    @staticmethod
    def planner_react(**options: Any) -> PlannerReActFlow:
        return PlannerReActFlow(**options)


async def _emit(
    store: EventStore,
    run_id: str,
    kind: str,
    payload: dict[str, object],
    observers: Sequence[Observer],
) -> Event:
    event = await store.emit(run_id, kind, payload)
    for observer in observers:
        try:
            result = observer(event)
            if inspect.isawaitable(result):
                await result
        except Exception:
            continue
    return event


def _executor_prompt(user_prompt: str, plan: Plan, step: PlanStep) -> str:
    return json.dumps(
        {"task": user_prompt, "goal": plan.goal, "step": step.description},
        ensure_ascii=False,
    )


def _review_prompt(user_prompt: str, plan: Plan, step: PlanStep) -> str:
    return json.dumps(
        {"task": user_prompt, "plan": plan.model_dump(mode="json"), "latest_step": step.id},
        ensure_ascii=False,
    )


__all__ = [
    "ChildRunRole",
    "Flow",
    "FlowChildRun",
    "FlowResult",
    "FlowRunHandle",
    "FlowRunNotCancellableError",
    "FlowStatus",
    "Plan",
    "PlannerAction",
    "PlannerDecision",
    "PlannerReActFlow",
    "PlannerReActResult",
    "PlanningLimits",
    "PlanStatus",
    "PlanStep",
    "PlanStepDraft",
    "PlanStepStatus",
    "RetryPolicy",
    "SequentialFlow",
    "Stage",
    "StageResult",
    "StepOutcome",
]
