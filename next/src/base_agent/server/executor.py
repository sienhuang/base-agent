"""Process-local execution backed by an authoritative persistent RunStore."""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from base_agent.agent import Agent
from base_agent.checkpoints import CheckpointNotFoundError
from base_agent.governance import ToolConfirmation
from base_agent.models import PendingInput, RunResult, RunStatus
from base_agent.run_handle import RunHandle, RunNotCancellableError
from base_agent.run_store import (
    AgentRunRecord,
    RunLease,
    RunLeaseLostError,
    RunRecordAlreadyExistsError,
    RunRecordNotFoundError,
    RunStore,
    RunTransitionError,
)

_RUN_TERMINAL_EVENTS = frozenset(
    {
        "run.succeeded",
        "run.failed",
        "run.cancelled",
        "run.blocked",
        "run.interrupted",
    }
)


class LocalRunExecutor:
    """Launch local work while persisting ownership and lifecycle transitions."""

    def __init__(
        self,
        agent: Agent,
        run_store: RunStore,
        *,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float | None = None,
        runtime_build_id: str | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("Run lease_seconds must be greater than zero")
        interval = heartbeat_interval_seconds or min(10.0, lease_seconds / 3)
        if interval <= 0 or interval >= lease_seconds:
            raise ValueError(
                "Run heartbeat_interval_seconds must be positive and shorter than the lease"
            )
        self.agent = agent
        self.run_store = run_store
        self.owner_id = owner_id or (
            f"local-executor:{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        )
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = interval
        self.runtime_build_id = runtime_build_id or agent.snapshot.fingerprint
        self._handles: dict[str, RunHandle] = {}
        self._supervisors: dict[str, asyncio.Task[AgentRunRecord]] = {}

    async def startup(self) -> tuple[AgentRunRecord, ...]:
        """Fence abandoned executions before accepting new work."""
        return await self.reconcile_expired()

    async def close(self) -> None:
        """Cancel locally active work, then stop any supervisors that did not drain."""
        supervisors = tuple(self._supervisors.values())
        cancellations = tuple(
            asyncio.create_task(self._handles[run_id].cancel())
            for run_id in tuple(self._supervisors)
            if run_id in self._handles
        )
        timeout = min(10.0, self.lease_seconds / 2)
        if cancellations:
            _, pending_cancellations = await asyncio.wait(cancellations, timeout=timeout)
            for cancellation_task in pending_cancellations:
                cancellation_task.cancel()
            await asyncio.gather(*cancellations, return_exceptions=True)
        if supervisors:
            _, pending_supervisors = await asyncio.wait(supervisors, timeout=timeout)
            for supervisor_task in pending_supervisors:
                supervisor_task.cancel()
            if pending_supervisors:
                await asyncio.gather(*pending_supervisors, return_exceptions=True)

    async def start(self, prompt: str, *, stream: bool = False) -> AgentRunRecord:
        await self.reconcile_expired()
        snapshot = self.agent.snapshot
        run_id = str(uuid4())
        record = AgentRunRecord(
            run_id=run_id,
            agent_id=snapshot.agent_id,
            agent_version=snapshot.agent_version,
            snapshot_fingerprint=snapshot.fingerprint,
            runtime_build_id=self.runtime_build_id,
        )
        lease = await self.run_store.create_claimed(
            record,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
        )
        try:
            handle = self.agent.start(prompt, stream=stream, run_id=run_id)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            failed = await self.run_store.settle(
                lease,
                status=RunStatus.FAILED,
                text="",
                error=error,
            )
            await self._emit_failure_if_missing(run_id, error)
            return failed
        self._handles[run_id] = handle
        supervisor = asyncio.create_task(
            self._supervise(handle, lease),
            name=f"base-agent-supervisor-{run_id}",
        )
        self._supervisors[run_id] = supervisor
        return await self.run_store.get(run_id)

    async def get(self, run_id: str) -> AgentRunRecord:
        await self.reconcile_expired()
        supervisor = self._supervisors.get(run_id)
        handle = self._handles.get(run_id)
        if supervisor is not None and (
            supervisor.done()
            or (handle is not None and handle.status is not RunStatus.RUNNING)
        ):
            with suppress(Exception):
                await asyncio.shield(supervisor)
        record = await self._record_or_adopt_waiting(run_id)
        self._validate_record_owner(record)
        return record

    def local_handle(self, run_id: str) -> RunHandle | None:
        return self._handles.get(run_id)

    async def resume(self, run_id: str, user_input: str) -> AgentRunRecord:
        return await self._continue(
            run_id,
            lambda: self.agent.resume(run_id, user_input),
        )

    async def confirm(
        self,
        run_id: str,
        confirmation: ToolConfirmation,
    ) -> AgentRunRecord:
        return await self._continue(
            run_id,
            lambda: self.agent.confirm(run_id, confirmation),
        )

    async def cancel(self, run_id: str) -> AgentRunRecord:
        await self.reconcile_expired()
        record = await self._record_or_adopt_waiting(run_id)
        self._validate_record_owner(record)
        if record.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        }:
            raise RunTransitionError(
                f"Run '{run_id}' already reached status '{record.status.value}' "
                "and cannot be cancelled"
            )
        if record.status is RunStatus.CANCELLED:
            return record
        record = await self.run_store.request_cancel(run_id)

        handle = self._handles.get(run_id)
        supervisor = self._supervisors.get(run_id)
        if record.status is RunStatus.RUNNING and handle is not None and supervisor is not None:
            await handle.cancel()
            return await supervisor

        if record.status is RunStatus.WAITING:
            self._validate_agent(record)
            lease = await self.run_store.claim(
                run_id,
                owner_id=self.owner_id,
                lease_seconds=self.lease_seconds,
            )
            result = await self.agent.cancel(run_id)
            return await self._settle_result(lease, result)

        if record.status in {RunStatus.CREATED, RunStatus.BLOCKED}:
            cancelled = await self.run_store.cancel_unowned(
                run_id,
                reason="run cancellation requested",
            )
            if record.checkpoint_revision is not None:
                with suppress(Exception):
                    await self.agent.checkpoint_store.delete(run_id)
            await self.agent.event_store.emit(
                run_id,
                "run.cancelled",
                {"error": cancelled.error, "source": "run_store"},
            )
            return cancelled

        # Another live executor owns this RUNNING Run. Its heartbeat observes the flag.
        return record

    async def reconcile_expired(self) -> tuple[AgentRunRecord, ...]:
        blocked = await self.run_store.block_expired(agent_id=self.agent.definition.id)
        for record in blocked:
            await self.agent.event_store.emit(
                record.run_id,
                "run.blocked",
                {
                    "error": record.error,
                    "reason": "owner_lease_expired",
                    "runtime_build_id": record.runtime_build_id,
                    "snapshot_fingerprint": record.snapshot_fingerprint,
                },
            )
        return blocked

    async def _continue(
        self,
        run_id: str,
        operation: Callable[[], Coroutine[Any, Any, RunResult]],
    ) -> AgentRunRecord:
        await self.reconcile_expired()
        record = await self._record_or_adopt_waiting(run_id)
        self._validate_agent(record)
        if record.status is not RunStatus.WAITING:
            raise ValueError(
                f"run '{run_id}' cannot continue from status '{record.status.value}'"
            )
        lease = await self.run_store.claim(
            run_id,
            owner_id=self.owner_id,
            lease_seconds=self.lease_seconds,
        )
        try:
            result, lease = await self._await_with_heartbeat(operation(), lease)
        except Exception:
            await self._restore_waiting_after_continuation_error(record, lease)
            raise
        return await self._settle_result(lease, result)

    async def _supervise(
        self,
        handle: RunHandle,
        lease: RunLease,
    ) -> AgentRunRecord:
        wait_task = asyncio.create_task(handle.wait())
        current_lease = lease
        try:
            while True:
                completed, _ = await asyncio.wait(
                    (wait_task,),
                    timeout=self.heartbeat_interval_seconds,
                )
                if wait_task in completed:
                    try:
                        result = await wait_task
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        failed = await self.run_store.settle(
                            current_lease,
                            status=RunStatus.FAILED,
                            text="",
                            error=error,
                        )
                        await self._emit_failure_if_missing(handle.run_id, error)
                        return failed
                    return await self._settle_result(current_lease, result)

                record = await self.run_store.get(handle.run_id)
                if record.cancel_requested:
                    with suppress(RunNotCancellableError):
                        await handle.cancel()
                    continue
                current_lease = await self.run_store.renew(
                    current_lease,
                    lease_seconds=self.lease_seconds,
                )
        except RunLeaseLostError:
            with suppress(Exception):
                await self.reconcile_expired()
            with suppress(Exception):
                await handle.cancel()
            return await self.run_store.get(handle.run_id)
        finally:
            if not wait_task.done():
                wait_task.cancel()
                with suppress(asyncio.CancelledError):
                    await wait_task
            self._supervisors.pop(handle.run_id, None)

    async def _await_with_heartbeat(
        self,
        operation: Coroutine[Any, Any, RunResult],
        lease: RunLease,
    ) -> tuple[RunResult, RunLease]:
        task = asyncio.create_task(operation)
        current_lease = lease
        try:
            while True:
                completed, _ = await asyncio.wait(
                    (task,),
                    timeout=self.heartbeat_interval_seconds,
                )
                if task in completed:
                    return await task, current_lease
                current_lease = await self.run_store.renew(
                    current_lease,
                    lease_seconds=self.lease_seconds,
                )
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    async def _settle_result(
        self,
        lease: RunLease,
        result: RunResult,
    ) -> AgentRunRecord:
        checkpoint_revision: int | None = None
        pending_input: dict[str, Any] | None = None
        if result.status is RunStatus.WAITING:
            checkpoint = await self.agent.checkpoint_store.load(result.run_id)
            checkpoint_revision = checkpoint.revision
            if result.pending_input is None:
                raise RuntimeError("WAITING Run result has no pending_input")
            pending_input = _pending_input_payload(result.pending_input)
        persisted_output = None if result.status is RunStatus.WAITING else result.output
        return await self.run_store.settle(
            lease,
            status=result.status,
            output=(
                persisted_output.model_dump(mode="json")
                if isinstance(persisted_output, BaseModel)
                else persisted_output
            ),
            text=(None if result.status is RunStatus.WAITING else result.text),
            error=(None if result.status is RunStatus.WAITING else result.error),
            pending_input=pending_input,
            checkpoint_revision=checkpoint_revision,
        )

    async def _restore_waiting_after_continuation_error(
        self,
        previous: AgentRunRecord,
        lease: RunLease,
    ) -> None:
        try:
            checkpoint = await self.agent.checkpoint_store.load(previous.run_id)
            await self.run_store.settle(
                lease,
                status=RunStatus.WAITING,
                pending_input=previous.pending_input,
                checkpoint_revision=checkpoint.revision,
            )
        except Exception:
            with suppress(Exception):
                failed = await self.run_store.settle(
                    lease,
                    status=RunStatus.FAILED,
                    text="",
                    error="continuation failed and its checkpoint could not be restored",
                )
                await self._emit_failure_if_missing(
                    previous.run_id,
                    failed.error or "continuation failed",
                )

    def _validate_agent(self, record: AgentRunRecord) -> None:
        self._validate_record_owner(record)
        snapshot = self.agent.snapshot
        if record.snapshot_fingerprint != snapshot.fingerprint:
            raise ValueError(
                f"run '{record.run_id}' cannot resume because the Agent snapshot changed"
            )

    def _validate_record_owner(self, record: AgentRunRecord) -> None:
        expected_agent_id = self.agent.definition.id
        if record.agent_id != expected_agent_id:
            raise RunRecordNotFoundError(
                f"Run record '{record.run_id}' does not belong to Agent "
                f"'{expected_agent_id}'"
            )

    async def _record_or_adopt_waiting(self, run_id: str) -> AgentRunRecord:
        try:
            return await self.run_store.get(run_id)
        except RunRecordNotFoundError as missing:
            try:
                checkpoint = await self.agent.checkpoint_store.load(run_id)
            except CheckpointNotFoundError:
                raise missing from None
            snapshot = self.agent.snapshot
            if checkpoint.agent_id != snapshot.agent_id:
                raise missing from None
            events = await self.agent.event_store.list(run_id)
            started = next(
                (event for event in events if event.kind == "run.started"),
                None,
            )
            historical_version = (
                started.payload.get("agent_version") if started is not None else None
            )
            pending = checkpoint.pending_input.public()
            adopted = AgentRunRecord(
                run_id=run_id,
                agent_id=checkpoint.agent_id,
                agent_version=(
                    historical_version
                    if isinstance(historical_version, str) and historical_version.strip()
                    else snapshot.agent_version
                ),
                snapshot_fingerprint=checkpoint.snapshot_fingerprint,
                runtime_build_id="legacy:unknown",
                status=RunStatus.WAITING,
                checkpoint_revision=checkpoint.revision,
                pending_input=_pending_input_payload(pending),
                created_at=checkpoint.created_at,
                updated_at=datetime.now(UTC),
            )
            try:
                return await self.run_store.create(adopted)
            except RunRecordAlreadyExistsError:
                return await self.run_store.get(run_id)

    async def _emit_failure_if_missing(self, run_id: str, error: str) -> None:
        events = await self.agent.event_store.list(run_id)
        if events and events[-1].kind in _RUN_TERMINAL_EVENTS:
            return
        await self.agent.event_store.emit(
            run_id,
            "run.failed",
            {"error": error, "source": "local_run_executor"},
        )


def _pending_input_payload(pending: PendingInput) -> dict[str, Any]:
    return {
        "request_id": pending.request_id,
        "kind": pending.kind.value,
        "tool_call_id": pending.tool_call_id,
        "tool_name": pending.tool_name,
        "prompt": pending.prompt,
        "metadata": dict(pending.metadata),
    }
