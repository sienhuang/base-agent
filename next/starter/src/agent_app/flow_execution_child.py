"""Isolated Flow handler protocol used by the shared ExecutionWorker."""

from __future__ import annotations

import asyncio
import sys
from contextlib import redirect_stdout
from typing import Any

from base_agent import AwaitHumanConfirmations, LocalArtifactStore
from base_agent.execution import (
    ExecutionAttempt,
    ExecutionLease,
    ExecutionOperation,
    ExecutionRequest,
    ExecutionStore,
    RecoveryStrategy,
)
from base_agent.flow_store import FlowCheckpoint, FlowRunRecord
from base_agent.flows import FlowStatus, PlannerReActFlow, PlannerReActResult
from base_agent.governance import ToolConfirmation
from base_agent.models import PendingInput, RunStatus
from base_agent.server.worker import ChildExecutionResult, FencedEventStore
from pydantic import BaseModel, ConfigDict

from agent_app.app import build_plan_react_flow
from agent_app.config import Settings
from agent_app.events import build_event_store_runtime
from agent_app.provider import build_plan_react_providers, build_provider


class FlowChildExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: ExecutionRequest
    attempt: ExecutionAttempt
    flow: FlowRunRecord
    lease: ExecutionLease


async def execute_child(payload: FlowChildExecutionInput) -> ChildExecutionResult:
    settings = Settings.from_env()
    if settings.event_store != "postgres":
        raise RuntimeError("independent Flow execution child requires PostgreSQL stores")
    runtime = build_event_store_runtime(settings)
    await runtime.start()
    try:
        fenced_events = FencedEventStore(
            runtime.store,
            runtime.executions,
            payload.lease,
        )

        async def save_checkpoint(checkpoint: FlowCheckpoint) -> FlowCheckpoint:
            return await runtime.executions.save_flow_checkpoint(
                payload.lease,
                checkpoint,
            )

        default_provider = build_provider(settings)
        planner_provider, executor_provider = build_plan_react_providers(
            settings,
            default_provider=default_provider,
        )
        flow = build_plan_react_flow(
            default_provider,
            planner_provider=planner_provider,
            executor_provider=executor_provider,
            planner_model=settings.plan_react_planner_model,
            executor_model=settings.plan_react_executor_model,
            artifact_store=LocalArtifactStore(settings.artifact_root),
            event_store=fenced_events,
            checkpoint_store=runtime.checkpoints,
            flow_checkpoint_writer=save_checkpoint,
            confirmation_policy=AwaitHumanConfirmations(),
        )
        mismatch = _definition_mismatch(flow, payload.flow)
        if mismatch is not None:
            return ChildExecutionResult(status=RunStatus.BLOCKED, error=mismatch)
        result = await _execute_operation(flow, payload, runtime.executions)
        checkpoint_revision: int | None = None
        pending_input: dict[str, Any] | None = None
        if result.status is FlowStatus.WAITING:
            checkpoint = await runtime.executions.load_flow_checkpoint(
                result.flow_run_id
            )
            checkpoint_revision = checkpoint.revision
            if result.pending_input is None:
                raise RuntimeError("WAITING Flow result has no pending input")
            pending_input = _pending_payload(result.pending_input)
        return ChildExecutionResult(
            status=RunStatus(result.status.value),
            output=None if result.status is FlowStatus.WAITING else result.output,
            text=None if result.status is FlowStatus.WAITING else result.output,
            error=None if result.status is FlowStatus.WAITING else result.error,
            pending_input=pending_input,
            checkpoint_revision=checkpoint_revision,
            waiting_child_run_id=result.waiting_child_run_id,
        )
    finally:
        await runtime.close()


async def _execute_operation(
    flow: PlannerReActFlow,
    payload: FlowChildExecutionInput,
    execution_store: ExecutionStore,
) -> PlannerReActResult:
    request = payload.request
    operation = request.operation
    request_payload = request.payload
    checkpoint: FlowCheckpoint | None = None
    if operation is ExecutionOperation.RECOVER:
        strategy = RecoveryStrategy(request_payload["recovery_strategy"])
        if strategy is RecoveryStrategy.RESTART:
            operation = ExecutionOperation.EXECUTE
        else:
            checkpoint = await execution_store.load_flow_checkpoint(
                payload.flow.flow_run_id
            )
            operation = ExecutionOperation(request_payload["recovery_operation"])

    with redirect_stdout(sys.stderr):
        if operation is ExecutionOperation.EXECUTE:
            prompt = request_payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("Flow execute request has no prompt")
            if checkpoint is not None:
                return await flow.recover(checkpoint)
            return await flow.run(
                prompt,
                stream=bool(request_payload.get("stream", False)),
                flow_run_id=payload.flow.flow_run_id,
            )

        checkpoint = checkpoint or await execution_store.load_flow_checkpoint(
            payload.flow.flow_run_id
        )
        if checkpoint.waiting_child_run_id is None:
            return await flow.recover(checkpoint)
        await flow.restore_checkpoint(checkpoint)
        if operation is ExecutionOperation.RESUME:
            user_input = request_payload.get("input")
            if not isinstance(user_input, str) or not user_input.strip():
                raise ValueError("Flow resume request has no input")
            return await flow.resume(payload.flow.flow_run_id, user_input)
        if operation is ExecutionOperation.CONFIRM:
            return await flow.confirm(
                payload.flow.flow_run_id,
                ToolConfirmation.model_validate(request_payload),
            )
    raise ValueError(f"unsupported Flow child operation '{operation.value}'")


def _definition_mismatch(
    flow: PlannerReActFlow,
    record: FlowRunRecord,
) -> str | None:
    if (
        record.flow_id == flow.id
        and record.flow_kind is flow.kind
        and record.definition_fingerprint == flow.definition_fingerprint
    ):
        return None
    return (
        "claimed Flow definition does not match this child Runtime; "
        "use a compatible AGENT_RUNTIME_ROUTE"
    )


def _pending_payload(pending: PendingInput) -> dict[str, Any]:
    return {
        "request_id": pending.request_id,
        "kind": pending.kind.value,
        "tool_call_id": pending.tool_call_id,
        "tool_name": pending.tool_name,
        "prompt": pending.prompt,
        "metadata": dict(pending.metadata),
    }


def main() -> None:
    raw = sys.stdin.buffer.read()
    payload = FlowChildExecutionInput.model_validate_json(raw)
    result = asyncio.run(execute_child(payload))
    sys.stdout.write(result.model_dump_json() + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
