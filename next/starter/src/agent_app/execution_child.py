"""Isolated Agent execution protocol used by the independent Worker Supervisor."""

from __future__ import annotations

import asyncio
import sys
from contextlib import redirect_stdout
from typing import Any

from base_agent import Agent, AwaitHumanConfirmations, LocalArtifactStore
from base_agent.execution import (
    ExecutionAttempt,
    ExecutionLease,
    ExecutionOperation,
    ExecutionRequest,
    RecoveryStrategy,
)
from base_agent.governance import ToolConfirmation
from base_agent.models import PendingInput, RunInvocation, RunResult, RunStatus
from base_agent.run_store import AgentRunRecord
from base_agent.server.worker import ChildExecutionResult, FencedEventStore
from pydantic import BaseModel, ConfigDict

from agent_app.app import build_agent
from agent_app.config import Settings
from agent_app.events import build_event_store_runtime
from agent_app.provider import build_provider


class ChildExecutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: ExecutionRequest
    attempt: ExecutionAttempt
    run: AgentRunRecord
    lease: ExecutionLease


async def execute_child(payload: ChildExecutionInput) -> ChildExecutionResult:
    settings = Settings.from_env()
    if settings.event_store != "postgres":
        raise RuntimeError("independent execution child requires PostgreSQL stores")
    runtime = build_event_store_runtime(settings)
    await runtime.start()
    try:
        fenced_events = FencedEventStore(
            runtime.store,
            runtime.executions,
            payload.lease,
        )
        agent = build_agent(
            build_provider(settings),
            artifact_store=LocalArtifactStore(settings.artifact_root),
            event_store=fenced_events,
            checkpoint_store=runtime.checkpoints,
            confirmation_policy=AwaitHumanConfirmations(),
        )
        snapshot = agent.snapshot
        if payload.run.snapshot_fingerprint != snapshot.fingerprint:
            return ChildExecutionResult(
                status=RunStatus.BLOCKED,
                error=(
                    "claimed Run snapshot does not match this child Runtime; "
                    "use a compatible AGENT_RUNTIME_ROUTE"
                ),
            )
        if (
            payload.request.operation is ExecutionOperation.RECOVER
            and RecoveryStrategy(
                payload.request.payload["recovery_strategy"]
            )
            is RecoveryStrategy.RESTART
        ):
            await runtime.checkpoints.delete(payload.run.run_id)
        result = await _execute_operation(agent, payload)
        checkpoint_revision: int | None = None
        pending_input: dict[str, Any] | None = None
        if result.status is RunStatus.WAITING:
            checkpoint = await runtime.checkpoints.load(result.run_id)
            checkpoint_revision = checkpoint.revision
            if result.pending_input is None:
                raise RuntimeError("WAITING child result has no pending input")
            pending_input = _pending_payload(result.pending_input)
        output = result.output
        if isinstance(output, BaseModel):
            output = output.model_dump(mode="json")
        return ChildExecutionResult(
            status=result.status,
            output=None if result.status is RunStatus.WAITING else output,
            text=None if result.status is RunStatus.WAITING else result.text,
            error=None if result.status is RunStatus.WAITING else result.error,
            pending_input=pending_input,
            checkpoint_revision=checkpoint_revision,
        )
    finally:
        await runtime.close()


async def _execute_operation(agent: Agent, payload: ChildExecutionInput) -> RunResult:
    request = payload.request
    operation = request.operation
    request_payload = request.payload
    if operation is ExecutionOperation.RECOVER:
        strategy = RecoveryStrategy(request_payload["recovery_strategy"])
        if strategy is RecoveryStrategy.RESTART:
            operation = ExecutionOperation.EXECUTE
        else:
            operation = ExecutionOperation(request_payload["recovery_operation"])
    with redirect_stdout(sys.stderr):
        if operation is ExecutionOperation.EXECUTE:
            prompt = request_payload.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("execute request has no prompt")
            handle = agent.start(
                prompt,
                stream=bool(request_payload.get("stream", False)),
                run_id=payload.run.run_id,
                invocation=RunInvocation(
                    invocation_id=f"run:{payload.run.run_id}",
                    attempt=payload.attempt.attempt_number,
                ),
            )
            return await handle.wait()
        if operation is ExecutionOperation.RESUME:
            user_input = request_payload.get("input")
            if not isinstance(user_input, str) or not user_input.strip():
                raise ValueError("resume request has no input")
            return await agent.resume(payload.run.run_id, user_input)
        if operation is ExecutionOperation.CONFIRM:
            return await agent.confirm(
                payload.run.run_id,
                ToolConfirmation.model_validate(request_payload),
            )
    raise ValueError(f"unsupported child operation '{operation.value}'")


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
    payload = ChildExecutionInput.model_validate_json(raw)
    result = asyncio.run(execute_child(payload))
    sys.stdout.write(result.model_dump_json() + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
