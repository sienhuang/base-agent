"""A deliberately small sequential Flow for simple multi-Agent applications."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from base_agent.agent import Agent
from base_agent.models import Event, RunResult, RunStatus
from base_agent.verification import VerificationEvidence

StageHandoff = Callable[[RunResult], str]
StageVerifier = Callable[
    [RunResult],
    bool | VerificationEvidence | Awaitable[bool | VerificationEvidence],
]


class FlowStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


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


class SequentialFlow:
    def __init__(self, *, id: str, stages: Sequence[Stage]) -> None:
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

    async def run(self, prompt: str) -> FlowResult:
        if not prompt.strip():
            raise ValueError("flow prompt must not be blank")
        flow_run_id = str(uuid4())
        events: list[Event] = []
        stage_results: list[StageResult] = []
        handoffs: dict[str, str] = {}

        _append_event(
            events,
            flow_run_id,
            "flow.started",
            flow_id=self.id,
            stage_count=len(self.stages),
        )
        for index, stage in enumerate(self.stages):
            stage_input = _select_stage_input(
                stage,
                index=index,
                user_input=prompt,
                handoffs=handoffs,
                previous=stage_results[-1].handoff if stage_results else None,
            )
            run_prompt = (
                f"{stage.prompt.strip()}\n\nInput:\n{stage_input}"
                if stage.prompt.strip()
                else stage_input
            )
            _append_event(
                events,
                flow_run_id,
                "stage.started",
                stage=stage.name,
                agent_id=stage.agent.definition.id,
            )
            run = await stage.agent.run(run_prompt)
            if run.status is RunStatus.FAILED:
                failed_stage = StageResult(
                    name=stage.name,
                    run=run,
                    handoff=run.text,
                )
                stage_results.append(failed_stage)
                _append_event(
                    events,
                    flow_run_id,
                    "stage.failed",
                    stage=stage.name,
                    agent_run_id=run.run_id,
                    error=run.error,
                )
                _append_event(events, flow_run_id, "flow.failed", stage=stage.name)
                return FlowResult(
                    flow_run_id=flow_run_id,
                    status=FlowStatus.FAILED,
                    output="",
                    stages=tuple(stage_results),
                    events=tuple(events),
                    error=f"Stage '{stage.name}' failed: {run.error}",
                )

            evidence, verification_error = await _verify_stage(stage, run)
            handoff = stage.handoff(run) if stage.handoff is not None else run.text
            result = StageResult(
                name=stage.name,
                run=run,
                handoff=handoff,
                evidence=evidence,
            )
            stage_results.append(result)
            handoffs[stage.name] = handoff
            if verification_error is not None:
                _append_event(
                    events,
                    flow_run_id,
                    "stage.verification_failed",
                    stage=stage.name,
                    agent_run_id=run.run_id,
                )
                _append_event(events, flow_run_id, "flow.failed", stage=stage.name)
                return FlowResult(
                    flow_run_id=flow_run_id,
                    status=FlowStatus.FAILED,
                    output="",
                    stages=tuple(stage_results),
                    events=tuple(events),
                    error=verification_error,
                )
            _append_event(
                events,
                flow_run_id,
                "stage.succeeded",
                stage=stage.name,
                agent_run_id=run.run_id,
            )

        _append_event(events, flow_run_id, "flow.succeeded")
        return FlowResult(
            flow_run_id=flow_run_id,
            status=FlowStatus.SUCCEEDED,
            output=stage_results[-1].handoff,
            stages=tuple(stage_results),
            events=tuple(events),
        )


class Flow:
    """Construction namespace; Strategy and Lifecycle stay out of the main path."""

    @staticmethod
    def sequence(*, id: str, stages: Sequence[Stage]) -> SequentialFlow:
        return SequentialFlow(id=id, stages=stages)


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


def _append_event(
    events: list[Event],
    flow_run_id: str,
    kind: str,
    **payload: Any,
) -> None:
    events.append(
        Event(
            sequence=len(events) + 1,
            kind=kind,
            run_id=flow_run_id,
            payload=MappingProxyType(dict(payload)),
        )
    )
