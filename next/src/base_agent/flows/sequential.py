"""A deliberately small sequential Flow for simple multi-Agent applications."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from base_agent.agent import Agent
from base_agent.events import EventEmitter, EventStore, InMemoryEventStore
from base_agent.flows.handle import FlowRunHandle
from base_agent.flows.models import FlowStatus
from base_agent.models import Event, RunResult, RunStatus
from base_agent.observability import Observer
from base_agent.run_handle import RunHandle, RunNotCancellableError
from base_agent.verification import VerificationEvidence

StageHandoff = Callable[[RunResult], str]
StageVerifier = Callable[
    [RunResult],
    bool | VerificationEvidence | Awaitable[bool | VerificationEvidence],
]

_UNFORWARDED_CHILD_EVENT_KINDS = frozenset({"model.tool_call.delta"})


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    agent: Agent
    prompt: str = ""
    input_from: str | None = None
    handoff: StageHandoff | None = None
    verify: StageVerifier | None = None

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
    evidence: tuple[VerificationEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowResult:
    flow_run_id: str
    status: FlowStatus
    output: str
    stages: tuple[StageResult, ...]
    events: tuple[Event, ...]
    error: str | None = None


@dataclass(slots=True)
class _SequentialFlowState:
    flow_run_id: str
    prompt: str
    emitter: EventEmitter
    stream: bool
    stage_results: list[StageResult] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)
    active_stage: Stage | None = None
    active_handle: RunHandle | None = None
    cancelled_stage: Stage | None = None
    cancelled_run: RunResult | None = None


class SequentialFlow:
    def __init__(
        self,
        *,
        id: str,
        stages: Sequence[Stage],
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        forward_child_events: bool = False,
    ) -> None:
        if not id.strip():
            raise ValueError("flow id must not be blank")
        if not stages:
            raise ValueError("SequentialFlow requires at least one Stage")
        stage_names = [stage.name for stage in stages]
        if len(set(stage_names)) != len(stage_names):
            raise ValueError("Flow Stage names must be unique")
        known_names: set[str] = set()
        for index, stage in enumerate(stages):
            if stage.input_from not in {None, "user"} and stage.input_from not in known_names:
                raise ValueError(
                    f"Stage '{stage.name}' references unavailable input '{stage.input_from}'"
                )
            if index == 0 and stage.input_from not in {None, "user"}:
                raise ValueError("the first Stage can only read the user input")
            known_names.add(stage.name)
        self.id = id
        self.stages = tuple(stages)
        self.event_store = event_store if event_store is not None else InMemoryEventStore()
        self.forward_child_events = forward_child_events
        self._observers = tuple(observers)

    async def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
    ) -> FlowResult:
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        state = self._new_state(prompt, stream=stream, on_event=on_event)
        return await self._execute(state)

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
    ) -> FlowRunHandle[FlowResult]:
        """Start one process-local Flow with replayable, live Events."""
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        state = self._new_state(prompt, stream=stream, on_event=on_event)

        async def operation() -> FlowResult:
            return await self._execute(state)

        async def cancel_operation() -> FlowResult:
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
        stream: bool,
        on_event: Observer | None,
    ) -> _SequentialFlowState:
        observers = self._observers if on_event is None else (*self._observers, on_event)
        flow_run_id = str(uuid4())
        return _SequentialFlowState(
            flow_run_id=flow_run_id,
            prompt=prompt,
            emitter=EventEmitter(flow_run_id, self.event_store, observers),
            stream=stream,
        )

    async def _execute(self, state: _SequentialFlowState) -> FlowResult:
        try:
            return await self._execute_stages(state)
        except asyncio.CancelledError:
            await self._cancel_active_child(state)
            raise
        except Exception as exc:
            return await self._failed_result(
                state,
                f"{type(exc).__name__}: {exc}",
            )

    async def _execute_stages(self, state: _SequentialFlowState) -> FlowResult:
        await state.emitter.emit(
            "flow.started",
            flow_id=self.id,
            stage_count=len(self.stages),
            stream=state.stream,
        )
        for index, stage in enumerate(self.stages):
            stage_input = _select_stage_input(
                stage,
                index=index,
                user_input=state.prompt,
                handoffs=state.handoffs,
                previous=(
                    state.stage_results[-1].handoff if state.stage_results else None
                ),
            )
            run_prompt = (
                f"{stage.prompt.strip()}\n\nInput:\n{stage_input}"
                if stage.prompt.strip()
                else stage_input
            )
            handle = self._start_child(state, stage=stage, prompt=run_prompt)
            await state.emitter.emit(
                "stage.started",
                stage=stage.name,
                agent_id=stage.agent.definition.id,
                agent_run_id=handle.run_id,
            )
            run = await handle.wait()
            state.active_stage = None
            state.active_handle = None
            if run.status is not RunStatus.SUCCEEDED:
                failed_stage = StageResult(
                    name=stage.name,
                    run=run,
                    handoff=run.text,
                )
                state.stage_results.append(failed_stage)
                await state.emitter.emit(
                    "stage.failed",
                    stage=stage.name,
                    agent_run_id=run.run_id,
                    error=run.error or f"Run ended with status '{run.status.value}'",
                )
                return await self._failed_result(
                    state,
                    f"Stage '{stage.name}' failed: "
                    f"{run.error or run.status.value}",
                    stage=stage.name,
                )

            evidence, verification_error = await _verify_stage(stage, run)
            handoff = stage.handoff(run) if stage.handoff is not None else run.text
            result = StageResult(
                name=stage.name,
                run=run,
                handoff=handoff,
                evidence=evidence,
            )
            state.stage_results.append(result)
            state.handoffs[stage.name] = handoff
            if verification_error is not None:
                await state.emitter.emit(
                    "stage.verification_failed",
                    stage=stage.name,
                    agent_run_id=run.run_id,
                    error=verification_error,
                )
                return await self._failed_result(
                    state,
                    verification_error,
                    stage=stage.name,
                )
            await state.emitter.emit(
                "stage.succeeded",
                stage=stage.name,
                agent_run_id=run.run_id,
            )

        await state.emitter.emit("flow.succeeded")
        return await self._result(state, FlowStatus.SUCCEEDED)

    def _start_child(
        self,
        state: _SequentialFlowState,
        *,
        stage: Stage,
        prompt: str,
    ) -> RunHandle:
        if state.active_handle is not None:
            raise RuntimeError("Flow already has an active child Run")

        async def forward_child_event(event: Event) -> None:
            if event.kind in _UNFORWARDED_CHILD_EVENT_KINDS:
                return
            payload = dict(event.payload)
            payload.update(
                {
                    "event_scope": "child_run",
                    "flow_run_id": state.flow_run_id,
                    "agent_run_id": event.run_id,
                    "stage": stage.name,
                    "child_sequence": event.sequence,
                    "child_kind": event.kind,
                }
            )
            await state.emitter.emit(
                _forwarded_child_kind(event.kind),
                turn=event.turn,
                **payload,
            )

        handle = stage.agent.start(
            prompt,
            stream=state.stream,
            on_event=(forward_child_event if self.forward_child_events else None),
        )
        state.active_stage = stage
        state.active_handle = handle
        return handle

    async def _cancel_active_child(self, state: _SequentialFlowState) -> None:
        stage = state.active_stage
        handle = state.active_handle
        if stage is None or handle is None:
            return
        try:
            run = await handle.cancel()
        except RunNotCancellableError:
            run = await handle.wait()
        state.stage_results.append(
            StageResult(name=stage.name, run=run, handoff=run.text)
        )
        state.cancelled_stage = stage
        state.cancelled_run = run
        state.active_stage = None
        state.active_handle = None

    async def _failed_result(
        self,
        state: _SequentialFlowState,
        error: str,
        *,
        stage: str | None = None,
    ) -> FlowResult:
        await state.emitter.emit("flow.failed", stage=stage, error=error)
        return await self._result(state, FlowStatus.FAILED, error=error)

    async def _cancelled_result(self, state: _SequentialFlowState) -> FlowResult:
        if state.cancelled_stage is not None and state.cancelled_run is not None:
            await state.emitter.emit(
                "stage.cancelled",
                stage=state.cancelled_stage.name,
                agent_run_id=state.cancelled_run.run_id,
            )
        await state.emitter.emit(
            "flow.cancelled",
            stage=(
                state.cancelled_stage.name if state.cancelled_stage is not None else None
            ),
        )
        return await self._result(state, FlowStatus.CANCELLED)

    async def _result(
        self,
        state: _SequentialFlowState,
        status: FlowStatus,
        *,
        error: str | None = None,
    ) -> FlowResult:
        return FlowResult(
            flow_run_id=state.flow_run_id,
            status=status,
            output=(state.stage_results[-1].handoff if state.stage_results else ""),
            stages=tuple(state.stage_results),
            events=await state.emitter.events(),
            error=error,
        )


def _select_stage_input(
    stage: Stage,
    *,
    index: int,
    user_input: str,
    handoffs: Mapping[str, str],
    previous: str | None,
) -> str:
    if stage.input_from == "user" or index == 0:
        return user_input
    if stage.input_from is not None:
        return handoffs[stage.input_from]
    if previous is None:
        return user_input
    return previous


async def _verify_stage(
    stage: Stage,
    run: RunResult,
) -> tuple[tuple[VerificationEvidence, ...], str | None]:
    if stage.verify is None:
        return (), None
    try:
        verdict = stage.verify(run)
        if inspect.isawaitable(verdict):
            verdict = await verdict
    except Exception as exc:
        evidence = (
            VerificationEvidence(
                verifier=f"stage:{stage.name}",
                scope="stage_output",
                status="failed",
                detail=f"{type(exc).__name__}: {exc}",
            ),
        )
        return evidence, f"Stage '{stage.name}' verifier failed: {exc}"
    if isinstance(verdict, VerificationEvidence):
        evidence = (verdict,)
        if verdict.status == "passed":
            return evidence, None
        return evidence, f"Stage '{stage.name}' failed verification: {verdict.detail}"
    evidence = (
        VerificationEvidence(
            verifier=f"stage:{stage.name}",
            scope="stage_output",
            status="passed" if verdict else "failed",
        ),
    )
    if verdict:
        return evidence, None
    return evidence, f"Stage '{stage.name}' failed verification"


def _forwarded_child_kind(kind: str) -> str:
    if kind.startswith(("run.", "message.")):
        return f"child.{kind}"
    return kind
