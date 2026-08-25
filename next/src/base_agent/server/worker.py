"""Independent Worker Supervisor with one subprocess per ExecutionAttempt."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, field_validator

from base_agent.events import EventStore
from base_agent.execution import (
    ClaimedExecution,
    ExecutionLease,
    ExecutionLeaseLostError,
    ExecutionStore,
    ExecutionTargetKind,
)
from base_agent.flows.models import FlowStatus
from base_agent.models import Event, RunStatus

_CHILD_TERMINAL_EVENTS = frozenset(
    {"run.waiting", "run.succeeded", "run.failed", "run.cancelled"}
)
_CHILD_FLOW_TERMINAL_EVENTS = frozenset(
    {"flow.waiting", "flow.succeeded", "flow.failed", "flow.cancelled"}
)


class ChildExecutionResult(BaseModel):
    """Bounded JSON result returned by the isolated child process."""

    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    output: Any | None = None
    text: str | None = None
    error: str | None = None
    pending_input: dict[str, Any] | None = None
    checkpoint_revision: int | None = None
    waiting_child_run_id: str | None = None

    @field_validator("status")
    @classmethod
    def status_is_settleable(cls, value: RunStatus) -> RunStatus:
        if value not in {
            RunStatus.WAITING,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.BLOCKED,
        }:
            raise ValueError(f"child cannot settle as '{value.value}'")
        return value


@runtime_checkable
class _FencedEventSink(Protocol):
    async def emit_fenced(
        self,
        lease: ExecutionLease,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event: ...


class FencedEventStore:
    """Reject late child Events and attach their authoritative Attempt identity."""

    def __init__(
        self,
        store: EventStore,
        execution_store: ExecutionStore,
        lease: ExecutionLease,
    ) -> None:
        self.store = store
        self.execution_store = execution_store
        self.lease = lease

    async def emit(
        self,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        turn: int | None = None,
    ) -> Event:
        enriched = dict(payload or {})
        enriched.setdefault("execution_attempt_id", self.lease.attempt_id)
        enriched.setdefault("execution_target_id", self.lease.target_id)
        persisted_kind = (
            f"execution.child.{kind.removeprefix('run.')}"
            if kind in _CHILD_TERMINAL_EVENTS and run_id == self.lease.target_id
            else (
                f"execution.child.flow.{kind.removeprefix('flow.')}"
                if kind in _CHILD_FLOW_TERMINAL_EVENTS
                and run_id == self.lease.target_id
                else kind
            )
        )
        if isinstance(self.store, _FencedEventSink):
            return await self.store.emit_fenced(
                self.lease,
                run_id,
                persisted_kind,
                enriched,
                turn=turn,
            )
        await self.execution_store.record_event_evidence(
            self.lease,
            kind,
            enriched,
            turn=turn,
        )
        return await self.store.emit(run_id, persisted_kind, enriched, turn=turn)

    async def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[Event, ...]:
        return await self.store.list(run_id, after_sequence=after_sequence)


@runtime_checkable
class ExecutionHandler(Protocol):
    """Target-specific child protocol and logical settlement adapter."""

    target_kind: ExecutionTargetKind
    child_command: tuple[str, ...]

    def child_input(
        self,
        claimed: ClaimedExecution,
        lease: ExecutionLease,
    ) -> Mapping[str, Any]: ...

    async def settle(
        self,
        store: ExecutionStore,
        lease: ExecutionLease,
        result: ChildExecutionResult,
    ) -> None: ...

    async def cancel(self, store: ExecutionStore, lease: ExecutionLease) -> None: ...


@dataclass(frozen=True, slots=True)
class AgentExecutionHandler:
    """Dispatch one Agent Run to an isolated Agent child process."""

    child_command: tuple[str, ...]
    target_kind: ExecutionTargetKind = ExecutionTargetKind.AGENT_RUN

    def __init__(self, child_command: Sequence[str]) -> None:
        if not child_command:
            raise ValueError("Agent child_command must not be empty")
        object.__setattr__(self, "child_command", tuple(child_command))
        object.__setattr__(self, "target_kind", ExecutionTargetKind.AGENT_RUN)

    def child_input(
        self,
        claimed: ClaimedExecution,
        lease: ExecutionLease,
    ) -> Mapping[str, Any]:
        return {
            "request": claimed.request.model_dump(mode="json"),
            "attempt": claimed.attempt.model_dump(mode="json"),
            "run": claimed.run.model_dump(mode="json"),
            "lease": lease.model_dump(mode="json"),
        }

    async def settle(
        self,
        store: ExecutionStore,
        lease: ExecutionLease,
        result: ChildExecutionResult,
    ) -> None:
        await store.settle(
            lease,
            status=result.status,
            output=result.output,
            text=result.text,
            error=result.error,
            pending_input=result.pending_input,
            checkpoint_revision=result.checkpoint_revision,
        )

    async def cancel(self, store: ExecutionStore, lease: ExecutionLease) -> None:
        await store.settle(
            lease,
            status=RunStatus.CANCELLED,
            text="",
            error="run cancellation requested",
        )


@dataclass(frozen=True, slots=True)
class FlowExecutionHandler:
    """Dispatch one Flow Run through the same Supervisor and Attempt lease."""

    child_command: tuple[str, ...]
    target_kind: ExecutionTargetKind = ExecutionTargetKind.FLOW_RUN

    def __init__(self, child_command: Sequence[str]) -> None:
        if not child_command:
            raise ValueError("Flow child_command must not be empty")
        object.__setattr__(self, "child_command", tuple(child_command))
        object.__setattr__(self, "target_kind", ExecutionTargetKind.FLOW_RUN)

    def child_input(
        self,
        claimed: ClaimedExecution,
        lease: ExecutionLease,
    ) -> Mapping[str, Any]:
        return {
            "request": claimed.request.model_dump(mode="json"),
            "attempt": claimed.attempt.model_dump(mode="json"),
            "flow": claimed.target.model_dump(mode="json"),
            "lease": lease.model_dump(mode="json"),
        }

    async def settle(
        self,
        store: ExecutionStore,
        lease: ExecutionLease,
        result: ChildExecutionResult,
    ) -> None:
        await store.settle(
            lease,
            status=FlowStatus(result.status.value),
            output=result.output,
            text=result.text,
            error=result.error,
            pending_input=result.pending_input,
            checkpoint_revision=result.checkpoint_revision,
            waiting_child_run_id=result.waiting_child_run_id,
        )

    async def cancel(self, store: ExecutionStore, lease: ExecutionLease) -> None:
        await store.settle(
            lease,
            status=FlowStatus.CANCELLED,
            text="",
            error="flow cancellation requested",
        )


class ExecutionWorker:
    """Supervise registered execution target handlers with shared Attempt leases."""

    def __init__(
        self,
        execution_store: ExecutionStore,
        *,
        child_command: Sequence[str] | None = None,
        handlers: Sequence[ExecutionHandler] = (),
        runtime_route: str = "default",
        runtime_build_id: str,
        owner_id: str | None = None,
        lease_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 10.0,
        poll_interval_seconds: float = 0.5,
        reconcile_interval_seconds: float = 5.0,
        child_env_remove: Sequence[str] = (),
        child_env_overrides: Mapping[str, str] | None = None,
    ) -> None:
        if child_command is not None and handlers:
            raise ValueError("provide child_command or handlers, not both")
        resolved_handlers: tuple[ExecutionHandler, ...]
        if handlers:
            resolved_handlers = tuple(handlers)
        elif child_command is not None:
            resolved_handlers = (
                cast(ExecutionHandler, AgentExecutionHandler(child_command)),
            )
        else:
            resolved_handlers = ()
        if not resolved_handlers:
            raise ValueError("ExecutionWorker requires at least one handler")
        handler_kinds = [handler.target_kind for handler in resolved_handlers]
        if len(set(handler_kinds)) != len(handler_kinds):
            raise ValueError("ExecutionWorker handler target kinds must be unique")
        if not runtime_route.strip() or not runtime_build_id.strip():
            raise ValueError("runtime route and build ID must not be blank")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be greater than zero")
        if heartbeat_interval_seconds <= 0 or heartbeat_interval_seconds >= lease_seconds:
            raise ValueError("heartbeat interval must be positive and shorter than the lease")
        if poll_interval_seconds <= 0 or reconcile_interval_seconds <= 0:
            raise ValueError("poll and reconcile intervals must be greater than zero")
        self.execution_store = execution_store
        self.handlers = {
            handler.target_kind: handler for handler in resolved_handlers
        }
        self.runtime_route = runtime_route
        self.runtime_build_id = runtime_build_id
        self.owner_id = owner_id or (
            f"worker:{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        )
        self.lease_seconds = lease_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.child_env_remove = tuple(child_env_remove)
        self.child_env_overrides = dict(child_env_overrides or {})
        if any(not name.strip() or "=" in name for name in self.child_env_remove):
            raise ValueError("child_env_remove entries must be environment variable names")
        if any(
            not name.strip() or "=" in name
            for name in self.child_env_overrides
        ):
            raise ValueError("child_env_overrides keys must be environment variable names")
        self._stopping = asyncio.Event()
        self._active_process: asyncio.subprocess.Process | None = None
        self._active_lease: ExecutionLease | None = None

    async def run_forever(self) -> None:
        """Poll until close() requests a graceful Worker shutdown."""
        loop = asyncio.get_running_loop()
        next_reconcile = loop.time()
        while not self._stopping.is_set():
            if loop.time() >= next_reconcile:
                await self.reconcile()
                next_reconcile = loop.time() + self.reconcile_interval_seconds
            claimed = await self.execution_store.claim_next(
                owner_id=self.owner_id,
                runtime_route=self.runtime_route,
                runtime_build_id=self.runtime_build_id,
                lease_seconds=self.lease_seconds,
                target_kinds=self.handlers,
            )
            if claimed is None:
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self.poll_interval_seconds,
                    )
                continue
            await self._execute_safely(claimed)

    async def run_once(self) -> bool:
        """Claim and finish at most one request; useful for tests and jobs."""
        await self.reconcile()
        claimed = await self.execution_store.claim_next(
            owner_id=self.owner_id,
            runtime_route=self.runtime_route,
            runtime_build_id=self.runtime_build_id,
            lease_seconds=self.lease_seconds,
            target_kinds=self.handlers,
        )
        if claimed is None:
            return False
        await self._execute_safely(claimed)
        return True

    async def reconcile(self) -> None:
        await self.execution_store.reconcile_expired()

    async def close(self) -> None:
        """Stop claiming and hand an active Attempt to explicit recovery."""
        self._stopping.set()
        process = self._active_process
        lease = self._active_lease
        if process is None or lease is None:
            return
        await _terminate_process(process)
        with suppress(ExecutionLeaseLostError):
            await self.execution_store.interrupt(
                lease,
                reason_code="worker_shutdown",
            )

    async def _execute_safely(self, claimed: ClaimedExecution) -> None:
        try:
            await self._execute(claimed)
        except asyncio.CancelledError:
            raise
        except Exception:
            with suppress(ExecutionLeaseLostError):
                await self.execution_store.interrupt(
                    self._active_lease or claimed.lease,
                    reason_code="supervisor_error",
                )
            raise

    async def _execute(self, claimed: ClaimedExecution) -> None:
        handler = self.handlers[claimed.request.target_kind]
        child_environment = os.environ.copy()
        for name in self.child_env_remove:
            child_environment.pop(name, None)
        child_environment.update(self.child_env_overrides)
        process = await asyncio.create_subprocess_exec(
            *handler.child_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=child_environment,
        )
        lease = claimed.lease
        self._active_process = process
        self._active_lease = lease
        try:
            await self.execution_store.mark_started(lease, child_pid=process.pid)
            child_input = json.dumps(
                handler.child_input(claimed, lease),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode()
            communicate = asyncio.create_task(process.communicate(child_input))
            current_lease = lease
            while True:
                done, _ = await asyncio.wait(
                    (communicate,),
                    timeout=self.heartbeat_interval_seconds,
                )
                if communicate in done:
                    stdout, stderr = await communicate
                    await self._settle_child(
                        claimed,
                        current_lease,
                        handler,
                        process.returncode,
                        stdout,
                        stderr,
                    )
                    return
                try:
                    heartbeat = await self.execution_store.renew(
                        current_lease,
                        lease_seconds=self.lease_seconds,
                    )
                except ExecutionLeaseLostError:
                    await _terminate_process(process)
                    return
                current_lease = heartbeat.lease
                self._active_lease = current_lease
                if heartbeat.cancel_requested:
                    await _terminate_process(process)
                    communicate.cancel()
                    with suppress(asyncio.CancelledError):
                        await communicate
                    await handler.cancel(self.execution_store, current_lease)
                    return
        except asyncio.CancelledError:
            await _terminate_process(process)
            with suppress(ExecutionLeaseLostError):
                await self.execution_store.interrupt(
                    self._active_lease or lease,
                    reason_code="worker_shutdown",
                )
            raise
        finally:
            self._active_process = None
            self._active_lease = None

    async def _settle_child(
        self,
        claimed: ClaimedExecution,
        lease: ExecutionLease,
        handler: ExecutionHandler,
        returncode: int | None,
        stdout: bytes,
        stderr: bytes,
    ) -> None:
        if returncode != 0:
            await self._interrupt_child(
                claimed,
                lease,
                reason_code="child_process_failed",
                detail=_bounded_stderr(stderr),
            )
            return
        try:
            line = next(line for line in reversed(stdout.splitlines()) if line.strip())
            result = ChildExecutionResult.model_validate_json(line)
        except (StopIteration, ValueError) as exc:
            await self._interrupt_child(
                claimed,
                lease,
                reason_code="child_protocol_error",
                detail=f"{type(exc).__name__}: {exc}",
            )
            return
        if result.status is RunStatus.BLOCKED:
            await self.execution_store.block(
                lease,
                reason_code="runtime_unavailable",
                error=result.error or "compatible Runtime is unavailable",
            )
            return
        await handler.settle(self.execution_store, lease, result)

    async def _interrupt_child(
        self,
        claimed: ClaimedExecution,
        lease: ExecutionLease,
        *,
        reason_code: str,
        detail: str,
    ) -> None:
        try:
            run = await self.execution_store.interrupt(lease, reason_code=reason_code)
        except ExecutionLeaseLostError:
            return
        del run, detail


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    _signal_process(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        _signal_process(process, signal.SIGKILL)
        await process.wait()


def _signal_process(process: asyncio.subprocess.Process, signum: signal.Signals) -> None:
    with suppress(ProcessLookupError):
        if os.name == "posix" and process.pid is not None:
            os.killpg(process.pid, signum)
        elif signum is signal.SIGTERM:
            process.terminate()
        else:
            process.kill()


def _bounded_stderr(stderr: bytes) -> str:
    text = stderr.decode(errors="replace")
    return text[-4096:] if text else "child exited without an error message"


def default_python_child_command(module: str) -> tuple[str, ...]:
    """Use the current environment's Python executable for a child module."""
    return (sys.executable, "-m", module)
