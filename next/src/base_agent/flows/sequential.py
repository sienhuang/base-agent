"""A deliberately small sequential Flow for simple multi-Agent applications."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from base_agent.agent import Agent
from base_agent.artifacts import (
    Artifact,
    ArtifactBinding,
    ArtifactBindingStatus,
    ArtifactRef,
    ArtifactSpec,
    artifact_spec,
)
from base_agent.events import EventEmitter, EventStore, InMemoryEventStore
from base_agent.flow_store import FlowCheckpoint, FlowKind
from base_agent.flows.checkpoints import (
    CheckpointWriter,
    callable_identity,
    definition_fingerprint,
    restore_dataclass,
    strict_json_value,
)
from base_agent.flows.handle import FlowRunHandle
from base_agent.flows.models import FlowStatus
from base_agent.governance import ToolConfirmation
from base_agent.models import Event, PendingInput, RunInvocation, RunResult, RunStatus
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
class RetryPolicy:
    """Retry a Stage when its Host verifier rejects an otherwise successful Run."""

    max_attempts: int = 1
    feedback_verification_failure: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")


@dataclass(frozen=True, slots=True)
class Stage:
    name: str
    agent: Agent
    prompt: str = ""
    input_from: str | None = None
    handoff: StageHandoff | None = None
    verify: StageVerifier | None = None
    retry: RetryPolicy = RetryPolicy()
    artifacts: tuple[ArtifactSpec, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("stage name must not be blank")
        if self.input_from is not None and not self.input_from.strip():
            raise ValueError("stage input_from must not be blank")
        normalized = tuple(artifact_spec(item) for item in self.artifacts)
        names = [item.name for item in normalized]
        if len(set(names)) != len(names):
            raise ValueError(f"Stage '{self.name}' ArtifactSpec names must be unique")
        object.__setattr__(self, "artifacts", normalized)


@dataclass(frozen=True, slots=True)
class StageResult:
    name: str
    run: RunResult
    handoff: str
    evidence: tuple[VerificationEvidence, ...] = ()
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class FlowResult:
    flow_run_id: str
    status: FlowStatus
    output: str
    stages: tuple[StageResult, ...]
    events: tuple[Event, ...]
    stage_attempts: tuple[StageResult, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    error: str | None = None
    artifact_specs: tuple[ArtifactSpec, ...] = ()
    artifact_bindings: tuple[ArtifactBinding, ...] = ()
    pending_input: PendingInput | None = None
    waiting_child_run_id: str | None = None


@dataclass(slots=True)
class _SequentialFlowState:
    flow_run_id: str
    prompt: str
    emitter: EventEmitter
    stream: bool
    stage_results: list[StageResult] = field(default_factory=list)
    stage_attempts: list[StageResult] = field(default_factory=list)
    handoffs: dict[str, str] = field(default_factory=dict)
    active_stage: Stage | None = None
    active_handle: RunHandle | None = None
    active_attempt: int | None = None
    cancelled_stage: Stage | None = None
    cancelled_run: RunResult | None = None
    cancelled_attempt: int | None = None
    accepted_artifacts: list[Artifact] = field(default_factory=list)
    artifact_bindings: list[ArtifactBinding] = field(default_factory=list)
    started: bool = False
    next_stage_index: int = 0
    next_attempt: int = 1
    retry_feedback: str | None = None
    waiting_stage: Stage | None = None
    waiting_run: RunResult | None = None
    waiting_attempt: int | None = None
    checkpoint_revision: int | None = None
    checkpoint_created_at: datetime | None = None


class SequentialFlow:
    def __init__(
        self,
        *,
        id: str,
        stages: Sequence[Stage],
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        forward_child_events: bool = False,
        artifact_specs: Sequence[ArtifactSpec | ArtifactRef] = (),
        artifacts: Sequence[ArtifactSpec | ArtifactRef] = (),
        checkpoint_writer: CheckpointWriter | None = None,
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
        self.kind = FlowKind.SEQUENTIAL
        self.stages = tuple(stages)
        self.event_store = event_store if event_store is not None else InMemoryEventStore()
        self.forward_child_events = forward_child_events
        declared_specs = (
            *(artifact_spec(item) for stage in stages for item in stage.artifacts),
            *(artifact_spec(item) for item in artifact_specs),
            *(artifact_spec(item) for item in artifacts),
        )
        spec_names = [item.name for item in declared_specs]
        if len(set(spec_names)) != len(spec_names):
            raise ValueError("Flow ArtifactSpec names must be unique")
        self.artifact_specs = tuple(declared_specs)
        self.checkpoint_writer = checkpoint_writer
        self.definition_fingerprint = definition_fingerprint(
            {
                "flow_id": self.id,
                "flow_kind": FlowKind.SEQUENTIAL.value,
                "stages": [
                    {
                        "name": stage.name,
                        "agent_snapshot": stage.agent.snapshot.fingerprint,
                        "prompt": stage.prompt,
                        "input_from": stage.input_from,
                        "handoff": callable_identity(stage.handoff),
                        "verify": callable_identity(stage.verify),
                        "retry": {
                            "max_attempts": stage.retry.max_attempts,
                            "feedback_verification_failure": (
                                stage.retry.feedback_verification_failure
                            ),
                        },
                        "artifacts": [
                            strict_json_value(item) for item in stage.artifacts
                        ],
                    }
                    for stage in self.stages
                ],
                "artifact_specs": [
                    strict_json_value(item) for item in self.artifact_specs
                ],
            }
        )
        self._observers = tuple(observers)
        self._waiting_states: dict[str, _SequentialFlowState] = {}
        self._waiting_lock = asyncio.Lock()

    async def run(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        flow_run_id: str | None = None,
    ) -> FlowResult:
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        state = self._new_state(
            prompt,
            stream=stream,
            on_event=on_event,
            flow_run_id=flow_run_id,
        )
        return await self._execute(state)

    async def resume(self, flow_run_id: str, user_input: str) -> FlowResult:
        """Resume the exact child Run currently suspending this Flow."""
        state = await self._claim_waiting_state(flow_run_id)
        stage = _require_waiting_stage(state)
        waiting = _require_waiting_run(state)
        async def forward_child_event(event: Event) -> None:
            await self._forward_child_event(state, stage, state.waiting_attempt or 1, event)
        try:
            run = await stage.agent.resume(
                waiting.run_id,
                user_input,
                on_event=(forward_child_event if self.forward_child_events else None),
            )
            await state.emitter.emit(
                "flow.resumed",
                stage=stage.name,
                agent_run_id=run.run_id,
                input_kind="tool_input",
            )
            return await self._execute(state, resumed_run=run)
        except BaseException:
            await self._restore_waiting_state(state)
            raise

    async def restore_checkpoint(self, checkpoint: FlowCheckpoint) -> None:
        """Restore one persisted safe boundary into this Flow instance."""
        state = self._state_from_checkpoint(checkpoint)
        async with self._waiting_lock:
            self._waiting_states[state.flow_run_id] = state

    async def recover(self, checkpoint: FlowCheckpoint) -> FlowResult:
        """Continue from the next durable Stage boundary without replaying prior Stages."""
        state = self._state_from_checkpoint(checkpoint)
        if state.waiting_run is not None:
            raise ValueError("a WAITING Flow checkpoint requires resume or confirm")
        return await self._execute(state)

    async def confirm(
        self,
        flow_run_id: str,
        confirmation: ToolConfirmation,
    ) -> FlowResult:
        """Resolve the exact child Tool confirmation suspending this Flow."""
        state = await self._claim_waiting_state(flow_run_id)
        stage = _require_waiting_stage(state)
        waiting = _require_waiting_run(state)
        async def forward_child_event(event: Event) -> None:
            await self._forward_child_event(state, stage, state.waiting_attempt or 1, event)
        try:
            run = await stage.agent.confirm(
                waiting.run_id,
                confirmation,
                on_event=(forward_child_event if self.forward_child_events else None),
            )
            await state.emitter.emit(
                "flow.resumed",
                stage=stage.name,
                agent_run_id=run.run_id,
                input_kind="tool_confirmation",
            )
            return await self._execute(state, resumed_run=run)
        except BaseException:
            await self._restore_waiting_state(state)
            raise

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        flow_run_id: str | None = None,
    ) -> FlowRunHandle[FlowResult]:
        """Start one process-local Flow with replayable, live Events."""
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        state = self._new_state(
            prompt,
            stream=stream,
            on_event=on_event,
            flow_run_id=flow_run_id,
        )

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
        flow_run_id: str | None,
    ) -> _SequentialFlowState:
        observers = self._observers if on_event is None else (*self._observers, on_event)
        if flow_run_id is not None and not flow_run_id.strip():
            raise ValueError("flow_run_id must not be blank")
        resolved_flow_run_id = flow_run_id if flow_run_id is not None else str(uuid4())
        return _SequentialFlowState(
            flow_run_id=resolved_flow_run_id,
            prompt=prompt,
            emitter=EventEmitter(resolved_flow_run_id, self.event_store, observers),
            stream=stream,
        )

    async def _execute(
        self,
        state: _SequentialFlowState,
        *,
        resumed_run: RunResult | None = None,
    ) -> FlowResult:
        try:
            return await self._execute_stages(state, resumed_run=resumed_run)
        except asyncio.CancelledError:
            await self._cancel_active_child(state)
            raise
        except Exception as exc:
            return await self._failed_result(
                state,
                f"{type(exc).__name__}: {exc}",
            )

    async def _execute_stages(
        self,
        state: _SequentialFlowState,
        *,
        resumed_run: RunResult | None,
    ) -> FlowResult:
        if not state.started:
            await state.emitter.emit(
                "flow.started",
                flow_id=self.id,
                stage_count=len(self.stages),
                stream=state.stream,
                artifact_specs=[
                    _artifact_spec_payload(artifact) for artifact in self.artifact_specs
                ],
            )
            state.started = True
        for index in range(state.next_stage_index, len(self.stages)):
            stage = self.stages[index]
            stage_input = _select_stage_input(
                stage,
                index=index,
                user_input=state.prompt,
                handoffs=state.handoffs,
                previous=(
                    state.stage_results[-1].handoff if state.stage_results else None
                ),
            )
            retry_feedback = state.retry_feedback
            first_attempt = state.next_attempt if index == state.next_stage_index else 1
            for attempt in range(first_attempt, stage.retry.max_attempts + 1):
                run_prompt = _stage_run_prompt(
                    stage,
                    stage_input=stage_input,
                    retry_feedback=retry_feedback,
                )
                if resumed_run is not None:
                    run = resumed_run
                    resumed_run = None
                else:
                    handle = self._start_child(
                        state,
                        stage=stage,
                        prompt=run_prompt,
                        attempt=attempt,
                    )
                    await state.emitter.emit(
                        "stage.started",
                        stage=stage.name,
                        agent_id=stage.agent.definition.id,
                        agent_run_id=handle.run_id,
                        attempt=attempt,
                        max_attempts=stage.retry.max_attempts,
                    )
                    run = await handle.wait()
                state.active_stage = None
                state.active_handle = None
                state.active_attempt = None
                if run.status is RunStatus.WAITING:
                    state.next_stage_index = index
                    state.next_attempt = attempt
                    state.retry_feedback = retry_feedback
                    state.waiting_stage = stage
                    state.waiting_run = run
                    state.waiting_attempt = attempt
                    async with self._waiting_lock:
                        self._waiting_states[state.flow_run_id] = state
                    pending = run.pending_input
                    await state.emitter.emit(
                        "stage.waiting",
                        stage=stage.name,
                        agent_run_id=run.run_id,
                        attempt=attempt,
                        request_id=(pending.request_id if pending is not None else None),
                        input_kind=(pending.kind.value if pending is not None else None),
                        prompt=(pending.prompt if pending is not None else None),
                        tool_name=(pending.tool_name if pending is not None else None),
                        tool_call_id=(pending.tool_call_id if pending is not None else None),
                        metadata=(dict(pending.metadata) if pending is not None else {}),
                    )
                    await state.emitter.emit(
                        "flow.waiting",
                        stage=stage.name,
                        agent_run_id=run.run_id,
                        attempt=attempt,
                        request_id=(pending.request_id if pending is not None else None),
                        input_kind=(pending.kind.value if pending is not None else None),
                    )
                    await self._persist_checkpoint(state)
                    return await self._result(state, FlowStatus.WAITING)
                state.waiting_stage = None
                state.waiting_run = None
                state.waiting_attempt = None
                if run.status is not RunStatus.SUCCEEDED:
                    failed_stage = StageResult(
                        name=stage.name,
                        run=run,
                        handoff=run.text,
                        attempt=attempt,
                    )
                    state.stage_attempts.append(failed_stage)
                    state.stage_results.append(failed_stage)
                    await state.emitter.emit(
                        "stage.failed",
                        stage=stage.name,
                        agent_run_id=run.run_id,
                        attempt=attempt,
                        error=(
                            run.error
                            or f"Run ended with status '{run.status.value}'"
                        ),
                    )
                    return await self._failed_result(
                        state,
                        f"Stage '{stage.name}' failed: "
                        f"{run.error or run.status.value}",
                        stage=stage.name,
                    )

                contract_evidence, contract_error = _verify_artifact_contract(stage, run)
                evidence, verification_error = await _verify_stage(stage, run)
                evidence = (*contract_evidence, *evidence)
                verification_error = contract_error or verification_error
                state.artifact_bindings.extend(
                    _artifact_bindings(
                        stage,
                        run,
                        attempt=attempt,
                        accepted=verification_error is None,
                        detail=verification_error or "",
                    )
                )
                result = StageResult(
                    name=stage.name,
                    run=run,
                    handoff=run.text,
                    evidence=evidence,
                    attempt=attempt,
                )
                state.stage_attempts.append(result)
                if verification_error is not None:
                    evidence_payload = [_evidence_payload(item) for item in evidence]
                    await state.emitter.emit(
                        "stage.verification_failed",
                        stage=stage.name,
                        agent_run_id=run.run_id,
                        attempt=attempt,
                        error=verification_error,
                        evidence=evidence_payload,
                    )
                    if attempt < stage.retry.max_attempts:
                        retry_feedback = (
                            _verification_retry_feedback(verification_error, evidence)
                            if stage.retry.feedback_verification_failure
                            else None
                        )
                        state.retry_feedback = retry_feedback
                        state.next_attempt = attempt + 1
                        await state.emitter.emit(
                            "stage.retrying",
                            stage=stage.name,
                            previous_agent_run_id=run.run_id,
                            attempt=attempt,
                            next_attempt=attempt + 1,
                            max_attempts=stage.retry.max_attempts,
                            error=verification_error,
                            evidence=evidence_payload,
                        )
                        await self._persist_checkpoint(state)
                        continue
                    state.stage_results.append(result)
                    return await self._failed_result(
                        state,
                        verification_error,
                        stage=stage.name,
                    )
                handoff = stage.handoff(run) if stage.handoff is not None else run.text
                result = StageResult(
                    name=stage.name,
                    run=run,
                    handoff=handoff,
                    evidence=evidence,
                    attempt=attempt,
                )
                state.stage_attempts[-1] = result
                state.stage_results.append(result)
                accepted_ids = {
                    binding.artifact_id
                    for binding in state.artifact_bindings
                    if binding.run_id == run.run_id
                    and binding.status is ArtifactBindingStatus.ACCEPTED
                    and binding.artifact_id is not None
                }
                state.accepted_artifacts.extend(
                    artifact for artifact in run.artifacts if artifact.id in accepted_ids
                )
                state.handoffs[stage.name] = handoff
                await state.emitter.emit(
                    "stage.succeeded",
                    stage=stage.name,
                    agent_run_id=run.run_id,
                    attempt=attempt,
                )
                state.next_stage_index = index + 1
                state.next_attempt = 1
                state.retry_feedback = None
                await self._persist_checkpoint(state)
                break

        await state.emitter.emit(
            "flow.succeeded",
            output=_sequential_output(state),
            child_runs=_sequential_child_runs(state),
        )
        return await self._result(state, FlowStatus.SUCCEEDED)

    async def _persist_checkpoint(self, state: _SequentialFlowState) -> None:
        writer = self.checkpoint_writer
        if writer is None:
            return
        now = datetime.now(UTC)
        created_at = state.checkpoint_created_at or now
        next_revision = (state.checkpoint_revision or 0) + 1
        next_stage = (
            self.stages[state.next_stage_index].name
            if state.next_stage_index < len(self.stages)
            else None
        )
        checkpoint = FlowCheckpoint(
            flow_run_id=state.flow_run_id,
            flow_id=self.id,
            flow_kind=FlowKind.SEQUENTIAL,
            definition_fingerprint=self.definition_fingerprint,
            revision=next_revision,
            cursor={
                "prompt": state.prompt,
                "stream": state.stream,
                "stage_results": strict_json_value(state.stage_results),
                "stage_attempts": strict_json_value(state.stage_attempts),
                "handoffs": strict_json_value(state.handoffs),
                "accepted_artifacts": strict_json_value(state.accepted_artifacts),
                "artifact_bindings": strict_json_value(state.artifact_bindings),
                "started": state.started,
                "next_stage_index": state.next_stage_index,
                "next_attempt": state.next_attempt,
                "retry_feedback": state.retry_feedback,
                "waiting_stage": (
                    state.waiting_stage.name
                    if state.waiting_stage is not None
                    else None
                ),
                "waiting_run": (
                    strict_json_value(state.waiting_run)
                    if state.waiting_run is not None
                    else None
                ),
                "waiting_attempt": state.waiting_attempt,
            },
            stage=next_stage or (
                state.waiting_stage.name
                if state.waiting_stage is not None
                else None
            ),
            waiting_child_run_id=(
                state.waiting_run.run_id if state.waiting_run is not None else None
            ),
            created_at=created_at,
            updated_at=now,
        )
        saved = await writer(checkpoint)
        state.checkpoint_revision = saved.revision
        state.checkpoint_created_at = saved.created_at

    def _state_from_checkpoint(
        self,
        checkpoint: FlowCheckpoint,
    ) -> _SequentialFlowState:
        if (
            checkpoint.flow_id != self.id
            or checkpoint.flow_kind is not FlowKind.SEQUENTIAL
            or checkpoint.definition_fingerprint != self.definition_fingerprint
        ):
            raise ValueError("Flow checkpoint does not match this Sequential Flow")
        cursor = checkpoint.cursor
        prompt = cursor.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Sequential Flow checkpoint has no prompt")
        stage_results = [
            restore_dataclass(StageResult, item)
            for item in _checkpoint_list(cursor, "stage_results")
        ]
        stage_attempts = [
            restore_dataclass(StageResult, item)
            for item in _checkpoint_list(cursor, "stage_attempts")
        ]
        waiting_stage_name = cursor.get("waiting_stage")
        waiting_stage = (
            next(
                (stage for stage in self.stages if stage.name == waiting_stage_name),
                None,
            )
            if isinstance(waiting_stage_name, str)
            else None
        )
        if isinstance(waiting_stage_name, str) and waiting_stage is None:
            raise ValueError(
                f"Sequential Flow checkpoint references unknown Stage "
                f"'{waiting_stage_name}'"
            )
        waiting_payload = cursor.get("waiting_run")
        waiting_run = (
            restore_dataclass(RunResult, waiting_payload)
            if waiting_payload is not None
            else None
        )
        if (waiting_stage is None) != (waiting_run is None):
            raise ValueError("Sequential Flow checkpoint has incomplete waiting state")
        if (
            waiting_run is not None
            and checkpoint.waiting_child_run_id != waiting_run.run_id
        ):
            raise ValueError("Sequential Flow checkpoint waiting child is inconsistent")
        if waiting_run is None and checkpoint.waiting_child_run_id is not None:
            raise ValueError("Sequential Flow checkpoint has an unexpected waiting child")
        return _SequentialFlowState(
            flow_run_id=checkpoint.flow_run_id,
            prompt=prompt,
            emitter=EventEmitter(
                checkpoint.flow_run_id,
                self.event_store,
                self._observers,
            ),
            stream=bool(cursor.get("stream", False)),
            stage_results=stage_results,
            stage_attempts=stage_attempts,
            handoffs=_checkpoint_string_mapping(cursor, "handoffs"),
            accepted_artifacts=[
                restore_dataclass(Artifact, item)
                for item in _checkpoint_list(cursor, "accepted_artifacts")
            ],
            artifact_bindings=[
                restore_dataclass(ArtifactBinding, item)
                for item in _checkpoint_list(cursor, "artifact_bindings")
            ],
            started=bool(cursor.get("started", True)),
            next_stage_index=_checkpoint_non_negative_int(
                cursor,
                "next_stage_index",
            ),
            next_attempt=_checkpoint_positive_int(cursor, "next_attempt"),
            retry_feedback=(
                cursor["retry_feedback"]
                if isinstance(cursor.get("retry_feedback"), str)
                else None
            ),
            waiting_stage=waiting_stage,
            waiting_run=waiting_run,
            waiting_attempt=(
                _checkpoint_positive_int(cursor, "waiting_attempt")
                if cursor.get("waiting_attempt") is not None
                else None
            ),
            checkpoint_revision=checkpoint.revision,
            checkpoint_created_at=checkpoint.created_at,
        )

    async def _claim_waiting_state(self, flow_run_id: str) -> _SequentialFlowState:
        async with self._waiting_lock:
            try:
                return self._waiting_states.pop(flow_run_id)
            except KeyError as exc:
                raise ValueError(
                    f"Flow Run '{flow_run_id}' is not waiting in this process"
                ) from exc

    async def _restore_waiting_state(self, state: _SequentialFlowState) -> None:
        async with self._waiting_lock:
            self._waiting_states[state.flow_run_id] = state

    def _start_child(
        self,
        state: _SequentialFlowState,
        *,
        stage: Stage,
        prompt: str,
        attempt: int,
    ) -> RunHandle:
        if state.active_handle is not None:
            raise RuntimeError("Flow already has an active child Run")

        async def forward_child_event(event: Event) -> None:
            await self._forward_child_event(state, stage, attempt, event)

        artifact_ids = _artifact_input_ids(state, stage)
        if artifact_ids:
            source_name = (
                stage.input_from
                if stage.input_from not in {None, "user"}
                else state.stage_results[-1].name
            )
            source_stage = next(item for item in self.stages if item.name == source_name)
            if source_stage.agent.artifact_store is not stage.agent.artifact_store:
                raise RuntimeError(
                    f"Stages '{source_stage.name}' and '{stage.name}' must share one "
                    "ArtifactStore to grant Artifact inputs"
                )

        handle = stage.agent.start(
            prompt,
            stream=state.stream,
            on_event=(forward_child_event if self.forward_child_events else None),
            invocation=RunInvocation(
                flow_run_id=state.flow_run_id,
                invocation_id=f"{state.flow_run_id}:{stage.name}:{attempt}",
                stage=stage.name,
                attempt=attempt,
                artifact_ids=artifact_ids,
            ),
        )
        state.active_stage = stage
        state.active_handle = handle
        state.active_attempt = attempt
        return handle

    async def _forward_child_event(
        self,
        state: _SequentialFlowState,
        stage: Stage,
        attempt: int,
        event: Event,
    ) -> None:
        if event.kind in _UNFORWARDED_CHILD_EVENT_KINDS:
            return
        payload = dict(event.payload)
        payload.update(
            {
                "event_scope": "child_run",
                "flow_run_id": state.flow_run_id,
                "agent_run_id": event.run_id,
                "stage": stage.name,
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

    async def _cancel_active_child(self, state: _SequentialFlowState) -> None:
        stage = state.active_stage
        handle = state.active_handle
        attempt = state.active_attempt
        if stage is None or handle is None or attempt is None:
            return
        try:
            run = await handle.cancel()
        except RunNotCancellableError:
            run = await handle.wait()
        result = StageResult(
            name=stage.name,
            run=run,
            handoff=run.text,
            attempt=attempt,
        )
        state.stage_attempts.append(result)
        state.stage_results.append(result)
        state.cancelled_stage = stage
        state.cancelled_run = run
        state.cancelled_attempt = attempt
        state.active_stage = None
        state.active_handle = None
        state.active_attempt = None

    async def _failed_result(
        self,
        state: _SequentialFlowState,
        error: str,
        *,
        stage: str | None = None,
    ) -> FlowResult:
        await state.emitter.emit(
            "flow.failed",
            stage=stage,
            error=error,
            output=_sequential_output(state),
            child_runs=_sequential_child_runs(state),
        )
        return await self._result(state, FlowStatus.FAILED, error=error)

    async def _cancelled_result(self, state: _SequentialFlowState) -> FlowResult:
        if state.waiting_stage is not None and state.waiting_run is not None:
            run = await state.waiting_stage.agent.cancel(state.waiting_run.run_id)
            cancelled = StageResult(
                name=state.waiting_stage.name,
                run=run,
                handoff=run.text,
                attempt=state.waiting_attempt or 1,
            )
            state.stage_attempts.append(cancelled)
            state.stage_results.append(cancelled)
            state.cancelled_stage = state.waiting_stage
            state.cancelled_run = run
            state.cancelled_attempt = state.waiting_attempt
            state.waiting_stage = None
            state.waiting_run = None
            state.waiting_attempt = None
            async with self._waiting_lock:
                self._waiting_states.pop(state.flow_run_id, None)
        if state.cancelled_stage is not None and state.cancelled_run is not None:
            await state.emitter.emit(
                "stage.cancelled",
                stage=state.cancelled_stage.name,
                agent_run_id=state.cancelled_run.run_id,
                attempt=state.cancelled_attempt,
            )
        await state.emitter.emit(
            "flow.cancelled",
            stage=(
                state.cancelled_stage.name if state.cancelled_stage is not None else None
            ),
            output=_sequential_output(state),
            child_runs=_sequential_child_runs(state),
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
            stage_attempts=tuple(state.stage_attempts),
            artifacts=_unique_artifacts(state.accepted_artifacts),
            error=error,
            artifact_specs=self.artifact_specs,
            artifact_bindings=tuple(state.artifact_bindings),
            pending_input=(
                state.waiting_run.pending_input if state.waiting_run is not None else None
            ),
            waiting_child_run_id=(
                state.waiting_run.run_id if state.waiting_run is not None else None
            ),
        )


def _sequential_output(state: _SequentialFlowState) -> str:
    return state.stage_results[-1].handoff if state.stage_results else ""


def _checkpoint_list(cursor: Mapping[str, object], name: str) -> list[object]:
    value = cursor.get(name, [])
    if not isinstance(value, list):
        raise ValueError(f"Sequential Flow checkpoint '{name}' must be a list")
    return value


def _checkpoint_string_mapping(
    cursor: Mapping[str, object],
    name: str,
) -> dict[str, str]:
    value = cursor.get(name, {})
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise ValueError(
            f"Sequential Flow checkpoint '{name}' must map strings to strings"
        )
    return dict(value)


def _checkpoint_non_negative_int(cursor: Mapping[str, object], name: str) -> int:
    value = cursor.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(
            f"Sequential Flow checkpoint '{name}' must be a non-negative integer"
        )
    return value


def _checkpoint_positive_int(cursor: Mapping[str, object], name: str) -> int:
    value = cursor.get(name)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(
            f"Sequential Flow checkpoint '{name}' must be a positive integer"
        )
    return value


def _require_waiting_stage(state: _SequentialFlowState) -> Stage:
    if state.waiting_stage is None:
        raise RuntimeError("waiting Sequential Flow has no Stage")
    return state.waiting_stage


def _require_waiting_run(state: _SequentialFlowState) -> RunResult:
    if state.waiting_run is None:
        raise RuntimeError("waiting Sequential Flow has no child Run")
    return state.waiting_run


def _sequential_child_runs(state: _SequentialFlowState) -> tuple[dict[str, object], ...]:
    stages = state.stage_attempts or state.stage_results
    return tuple(
        {
            "role": "stage",
            "phase": stage.name,
            "agent_run_id": stage.run.run_id,
            "status": stage.run.status.value,
            "source_plan_revision": None,
            "target_plan_revision": None,
            "step_id": None,
            "attempt": stage.attempt,
        }
        for stage in stages
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


def _stage_run_prompt(
    stage: Stage,
    *,
    stage_input: str,
    retry_feedback: str | None,
) -> str:
    stage_prompt = stage.prompt.strip()
    sections = [stage_prompt, f"Input:\n{stage_input}" if stage_prompt else stage_input]
    if retry_feedback is not None:
        sections.append(retry_feedback)
    return "\n\n".join(section for section in sections if section)


def _verification_retry_feedback(
    error: str,
    evidence: tuple[VerificationEvidence, ...],
) -> str:
    details = "\n".join(
        f"- verifier={item.verifier}; scope={item.scope}; "
        f"status={item.status}; detail={item.detail or '(none)'}"
        for item in evidence
    )
    return (
        "Host verification rejected the previous attempt. Correct the output and "
        "execute the required Tools again before returning.\n"
        f"Verification error: {error}\n"
        f"Evidence:\n{details}"
    )


def _evidence_payload(evidence: VerificationEvidence) -> dict[str, str]:
    return {
        "verifier": evidence.verifier,
        "scope": evidence.scope,
        "status": evidence.status,
        "detail": evidence.detail,
    }


def _artifact_spec_payload(artifact: ArtifactSpec) -> dict[str, object]:
    return {
        "name": artifact.name,
        "location": artifact.location,
        "media_type": artifact.media_type,
        "required": artifact.required,
        "max_bytes": artifact.max_bytes,
    }


def _verify_artifact_contract(
    stage: Stage,
    run: RunResult,
) -> tuple[tuple[VerificationEvidence, ...], str | None]:
    if not stage.artifacts:
        return (), None
    produced = {artifact.name: artifact for artifact in run.artifacts}
    evidence: list[VerificationEvidence] = []
    errors: list[str] = []
    for spec in stage.artifacts:
        artifact = produced.get(spec.name)
        error: str | None = None
        if artifact is None:
            if spec.required:
                error = "required Artifact was not produced"
        elif artifact.media_type != spec.media_type:
            error = (
                f"media_type is '{artifact.media_type}', expected '{spec.media_type}'"
            )
        elif spec.location is not None and artifact.location != spec.location:
            error = (
                f"location is '{artifact.location}', expected '{spec.location}'"
            )
        elif spec.max_bytes is not None and artifact.size_bytes > spec.max_bytes:
            error = (
                f"size {artifact.size_bytes} exceeds max_bytes={spec.max_bytes}"
            )
        if error is not None:
            errors.append(f"{spec.name}: {error}")
        evidence.append(
            VerificationEvidence(
                verifier="artifact_contract",
                scope=spec.name,
                status="failed" if error is not None else "passed",
                detail=error or "",
            )
        )
    if errors:
        return tuple(evidence), (
            f"Stage '{stage.name}' failed Artifact contract: {'; '.join(errors)}"
        )
    return tuple(evidence), None


def _artifact_input_ids(
    state: _SequentialFlowState,
    stage: Stage,
) -> tuple[str, ...]:
    if not state.stage_results or stage.input_from == "user":
        return ()
    if stage.input_from is None:
        source = state.stage_results[-1]
    else:
        source = next(
            result for result in state.stage_results if result.name == stage.input_from
        )
    accepted_ids = {
        binding.artifact_id
        for binding in state.artifact_bindings
        if binding.run_id == source.run.run_id
        and binding.status is ArtifactBindingStatus.ACCEPTED
        and binding.artifact_id is not None
    }
    return tuple(
        artifact.id for artifact in source.run.artifacts if artifact.id in accepted_ids
    )


def _unique_artifacts(artifacts: Sequence[Artifact]) -> tuple[Artifact, ...]:
    by_id: dict[str, Artifact] = {}
    for artifact in artifacts:
        by_id[artifact.id] = artifact
    return tuple(by_id.values())


def _artifact_bindings(
    stage: Stage,
    run: RunResult,
    *,
    attempt: int,
    accepted: bool,
    detail: str,
) -> tuple[ArtifactBinding, ...]:
    produced = {artifact.name: artifact for artifact in run.artifacts}
    bindings: list[ArtifactBinding] = []
    for spec in stage.artifacts:
        artifact = produced.get(spec.name)
        binding_accepted = accepted and artifact is not None
        bindings.append(
            ArtifactBinding(
                spec=spec,
                artifact_id=artifact.id if artifact is not None else None,
                status=(
                    ArtifactBindingStatus.ACCEPTED
                    if binding_accepted
                    else ArtifactBindingStatus.REJECTED
                ),
                stage=stage.name,
                run_id=run.run_id,
                attempt=attempt,
                detail=detail,
            )
        )
    return tuple(bindings)


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
