"""Read-only HTTP projections rebuilt from persisted execution Events."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from base_agent.flows import FlowStatus
from base_agent.models import Event, RunStatus
from base_agent.server.schemas import (
    ArtifactRefResponse,
    FlowChildRunResponse,
    FlowResponse,
    RunResponse,
)

ExecutionKind = Literal["run", "flow"]

_RUN_ROOT_KINDS = frozenset(
    {"run.started", "run.succeeded", "run.failed", "run.cancelled"}
)
_FLOW_ROOT_KINDS = frozenset(
    {"flow.started", "flow.succeeded", "flow.failed", "flow.cancelled"}
)
_RUN_TERMINAL_STATUSES = {
    "run.succeeded": RunStatus.SUCCEEDED,
    "run.failed": RunStatus.FAILED,
    "run.cancelled": RunStatus.CANCELLED,
}
_FLOW_TERMINAL_STATUSES = {
    "flow.succeeded": FlowStatus.SUCCEEDED,
    "flow.failed": FlowStatus.FAILED,
    "flow.cancelled": FlowStatus.CANCELLED,
}


def is_execution_history(events: tuple[Event, ...], kind: ExecutionKind) -> bool:
    """Reject an ID from the other execution namespace."""
    expected = _RUN_ROOT_KINDS if kind == "run" else _FLOW_ROOT_KINDS
    return any(event.kind in expected for event in events)


def project_run_response(run_id: str, events: tuple[Event, ...]) -> RunResponse:
    """Rebuild the public Run response without a process-local RunHandle."""
    terminal = events[-1]
    status = _RUN_TERMINAL_STATUSES.get(terminal.kind, RunStatus.RUNNING)
    completed = _last_event(events, "message.completed")
    completed_text = (
        _optional_string(completed.payload.get("content"))
        if completed is not None
        else None
    )
    if status is RunStatus.RUNNING:
        output: Any | None = None
        text: str | None = None
        error = None
    elif status is RunStatus.SUCCEEDED:
        text = _optional_string(terminal.payload.get("text"))
        if text is None:
            text = completed_text or ""
        output = terminal.payload.get("output", text)
        error = None
    else:
        output = None
        text = _optional_string(terminal.payload.get("text")) or ""
        error = _terminal_error(terminal)
    return RunResponse(
        run_id=run_id,
        status=status,
        event_count=len(events),
        output=output,
        text=text,
        error=error,
        artifacts=_artifact_responses(events),
    )


def project_flow_response(
    flow_run_id: str,
    events: tuple[Event, ...],
) -> FlowResponse:
    """Rebuild the public Flow response without a process-local FlowRunHandle."""
    terminal = events[-1]
    status = _FLOW_TERMINAL_STATUSES.get(terminal.kind, FlowStatus.RUNNING)
    output = _flow_output(events, terminal, status)
    error = (
        _terminal_error(terminal)
        if status in {FlowStatus.FAILED, FlowStatus.CANCELLED}
        else None
    )
    plan = _flow_plan(events, terminal, status)
    return FlowResponse(
        flow_run_id=flow_run_id,
        plan_id=_plan_id(events),
        status=status,
        event_count=len(events),
        output=output,
        error=error,
        plan=plan,
        child_runs=_flow_child_runs(events, terminal),
        artifacts=_flow_artifact_responses(events),
    )


def _flow_output(
    events: tuple[Event, ...],
    terminal: Event,
    status: FlowStatus,
) -> str | None:
    if status is FlowStatus.RUNNING:
        return None
    persisted = _optional_string(terminal.payload.get("output"))
    if persisted is not None:
        return persisted
    for event in reversed(events):
        if event.kind == "message.completed" and event.payload.get("event_scope") == "flow":
            return _optional_string(event.payload.get("content")) or ""
    successful_stage_run_ids = [
        _optional_string(event.payload.get("agent_run_id"))
        for event in events
        if event.kind == "stage.succeeded"
    ]
    final_stage_run_id = next(
        (run_id for run_id in reversed(successful_stage_run_ids) if run_id is not None),
        None,
    )
    if final_stage_run_id is not None:
        for event in reversed(events):
            if (
                event.kind in {"message.completed", "child.message.completed"}
                and event.payload.get("agent_run_id") == final_stage_run_id
            ):
                return _optional_string(event.payload.get("content")) or ""
    return "" if status is FlowStatus.SUCCEEDED else None


def _flow_plan(
    events: tuple[Event, ...],
    terminal: Event,
    status: FlowStatus,
) -> dict[str, Any] | None:
    persisted = terminal.payload.get("plan")
    if isinstance(persisted, dict):
        return deepcopy(persisted)
    plan: dict[str, Any] | None = None
    for event in events:
        candidate = event.payload.get("plan")
        if event.kind in {"plan.created", "plan.revised"} and isinstance(candidate, dict):
            plan = deepcopy(candidate)
        if plan is None:
            continue
        if event.kind in {"step.started", "step.completed", "step.failed", "step.cancelled"}:
            _apply_step_event(plan, event, events)
    if plan is None:
        return None
    plan["status"] = {
        FlowStatus.RUNNING: "running",
        FlowStatus.SUCCEEDED: "succeeded",
        FlowStatus.FAILED: "failed",
        FlowStatus.CANCELLED: "cancelled",
    }[status]
    return plan


def _apply_step_event(
    plan: dict[str, Any],
    event: Event,
    events: tuple[Event, ...],
) -> None:
    steps = plan.get("steps")
    step_id = event.payload.get("step_id")
    if not isinstance(steps, list) or not isinstance(step_id, str):
        return
    step = next(
        (item for item in steps if isinstance(item, dict) and item.get("id") == step_id),
        None,
    )
    if step is None:
        return
    attempt = _optional_int(event.payload.get("attempt"))
    if attempt is not None:
        step["attempt_count"] = attempt
    if event.kind == "step.started":
        step["status"] = "running"
        return
    if event.kind == "step.cancelled":
        step["status"] = "cancelled"
        return
    success = event.kind == "step.completed" and event.payload.get("success", True) is True
    step["status"] = "succeeded" if success else "failed"
    outcome = _legacy_step_outcome(events, event)
    outcome.update({
        "success": success,
        "summary": str(event.payload.get("summary", outcome.get("summary", event.kind))),
        "error": _optional_string(event.payload.get("error")),
    })
    step["outcome"] = outcome


def _legacy_step_outcome(
    events: tuple[Event, ...],
    terminal_step: Event,
) -> dict[str, Any]:
    agent_run_id = _optional_string(terminal_step.payload.get("agent_run_id"))
    step_id = _optional_string(terminal_step.payload.get("step_id"))
    parsed: dict[str, Any] = {}
    if agent_run_id is not None:
        for event in reversed(events):
            if event.sequence >= terminal_step.sequence:
                continue
            if (
                event.kind == "child.message.completed"
                and event.payload.get("agent_run_id") == agent_run_id
            ):
                content = event.payload.get("content")
                if isinstance(content, str):
                    try:
                        candidate = json.loads(content)
                    except json.JSONDecodeError:
                        candidate = None
                    if isinstance(candidate, dict):
                        parsed = candidate
                        break
    artifact_ids = [
        artifact_id
        for event in events
        if event.kind == "artifact.selected"
        and event.payload.get("step_id") == step_id
        and event.sequence <= terminal_step.sequence
        if (artifact_id := _optional_string(event.payload.get("artifact_id"))) is not None
    ]
    return {
        "success": bool(parsed.get("success", False)),
        "summary": str(parsed.get("summary", terminal_step.kind)),
        "output": parsed.get("output"),
        "error": _optional_string(parsed.get("error")),
        "artifact_ids": artifact_ids or list(_string_tuple(parsed.get("artifact_ids"))),
    }


def _flow_child_runs(
    events: tuple[Event, ...],
    terminal: Event,
) -> tuple[FlowChildRunResponse, ...]:
    persisted = terminal.payload.get("child_runs")
    if isinstance(persisted, (list, tuple)):
        return tuple(
            FlowChildRunResponse.model_validate(item)
            for item in persisted
            if isinstance(item, dict)
        )

    children: dict[str, dict[str, Any]] = {}
    for event in events:
        payload = event.payload
        planner_run_id = _optional_string(payload.get("planner_run_id"))
        if planner_run_id is not None and planner_run_id in children:
            planner_child = children[planner_run_id]
            if event.kind == "plan.created":
                planner_child["target_plan_revision"] = _optional_int(
                    payload.get("plan_revision")
                )
            elif event.kind == "plan.revised":
                planner_child["target_plan_revision"] = _optional_int(
                    payload.get("target_plan_revision")
                )
            elif event.kind == "plan.reviewed":
                planner_child["target_plan_revision"] = _optional_int(
                    payload.get("plan_revision")
                )
        agent_run_id = _optional_string(payload.get("agent_run_id"))
        if event.kind == "planner.started" and agent_run_id is not None:
            children.setdefault(
                agent_run_id,
                _child_payload(
                    role="planner",
                    phase=str(payload.get("phase", "review")),
                    agent_run_id=agent_run_id,
                    source_plan_revision=_optional_int(payload.get("source_plan_revision")),
                    step_id=_optional_string(payload.get("step_id")),
                    attempt=_optional_int(payload.get("attempt")),
                ),
            )
        elif event.kind == "step.started" and agent_run_id is not None:
            children.setdefault(
                agent_run_id,
                _child_payload(
                    role="executor",
                    phase="execute",
                    agent_run_id=agent_run_id,
                    source_plan_revision=_optional_int(payload.get("plan_revision")),
                    step_id=_optional_string(payload.get("step_id")),
                    attempt=_optional_int(payload.get("attempt")),
                ),
            )
        elif event.kind == "stage.started" and agent_run_id is not None:
            children.setdefault(
                agent_run_id,
                _child_payload(
                    role="stage",
                    phase=str(payload.get("stage", "stage")),
                    agent_run_id=agent_run_id,
                    source_plan_revision=None,
                    step_id=None,
                    attempt=_optional_int(payload.get("attempt")),
                ),
            )

        if agent_run_id is None or agent_run_id not in children:
            continue
        child = children[agent_run_id]
        child_kind = payload.get("child_kind")
        if child_kind == "run.succeeded":
            child["status"] = RunStatus.SUCCEEDED
        elif child_kind == "run.failed":
            child["status"] = RunStatus.FAILED
        elif child_kind == "run.cancelled":
            child["status"] = RunStatus.CANCELLED
        if event.kind in {"planner.completed", "step.completed", "stage.succeeded"}:
            child["status"] = RunStatus.SUCCEEDED
        elif event.kind in {"step.failed", "stage.failed"}:
            child["status"] = RunStatus.FAILED
        elif event.kind in {"planner.cancelled", "step.cancelled", "stage.cancelled"}:
            child["status"] = RunStatus.CANCELLED

    return tuple(FlowChildRunResponse.model_validate(item) for item in children.values())


def _child_payload(
    *,
    role: str,
    phase: str,
    agent_run_id: str,
    source_plan_revision: int | None,
    step_id: str | None,
    attempt: int | None,
) -> dict[str, Any]:
    return {
        "role": role,
        "phase": phase,
        "agent_run_id": agent_run_id,
        "status": RunStatus.RUNNING,
        "source_plan_revision": source_plan_revision,
        "target_plan_revision": None,
        "step_id": step_id,
        "attempt": attempt,
    }


def _plan_id(events: tuple[Event, ...]) -> str | None:
    for event in events:
        plan_id = _optional_string(event.payload.get("plan_id"))
        if plan_id is not None:
            return plan_id
    return None


def _flow_artifact_responses(
    events: tuple[Event, ...],
) -> tuple[ArtifactRefResponse, ...]:
    event_kind = "artifact.selected" if _plan_id(events) is not None else "artifact.created"
    return _artifact_responses(events, event_kind=event_kind)


def _artifact_responses(
    events: tuple[Event, ...],
    *,
    event_kind: str = "artifact.created",
) -> tuple[ArtifactRefResponse, ...]:
    responses: dict[str, ArtifactRefResponse] = {}
    for event in events:
        if event.kind != event_kind:
            continue
        artifact = ArtifactRefResponse(
            id=_optional_string(event.payload.get("artifact_id")),
            name=str(event.payload.get("name", "")),
            location=_optional_string(event.payload.get("location")),
            media_type=str(event.payload.get("media_type", "application/octet-stream")),
            size_bytes=_optional_int(event.payload.get("size_bytes")),
            sha256=_optional_string(event.payload.get("sha256")),
            created_at=_optional_datetime(event.payload.get("created_at")),
            producer_run_id=_optional_string(event.payload.get("producer_run_id")),
            producer_agent_id=_optional_string(event.payload.get("producer_agent_id")),
            flow_run_id=_optional_string(event.payload.get("flow_run_id")),
            stage=_optional_string(event.payload.get("stage")),
            tool_call_id=_optional_string(event.payload.get("tool_call_id")),
            source_artifact_ids=_string_tuple(event.payload.get("source_artifact_ids")),
        )
        responses[artifact.id or artifact.name] = artifact
    return tuple(responses.values())


def _last_event(events: tuple[Event, ...], kind: str) -> Event | None:
    return next((event for event in reversed(events) if event.kind == kind), None)


def _terminal_error(event: Event) -> str | None:
    error = event.payload.get("error") or event.payload.get("reason")
    return str(error) if error is not None else None


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, str))
