"""Application composition and DSH Runtime lifecycle ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from dsh_base_agent._dsh import (
    DshEventHandler,
    DshInput,
    DshRunResult,
    DshRuntime,
    DshRuntimeFactory,
    RuntimeSpec,
)
from dsh_base_agent.context import ContextRuntime
from dsh_base_agent.memory import Memory, MemoryRuntime
from dsh_base_agent.state import LocalState
from dsh_base_agent.tool_gateway import ToolGateway


@dataclass(slots=True)
class _RuntimeEntry:
    runtime: DshRuntime
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    started: bool = False


class BaseAgentApp:
    """Own shared control-plane services and one DSH Runtime per Agent snapshot."""

    def __init__(
        self,
        *,
        state: LocalState,
        runtime_factory: DshRuntimeFactory,
        memory: Memory | None = None,
    ) -> None:
        self.state = state
        self.event_store = state.event_store
        self.memory = MemoryRuntime(memory, self.event_store)
        self.context = ContextRuntime(self.event_store)
        self._runtime_factory = runtime_factory
        self._runtimes: dict[str, _RuntimeEntry] = {}
        self._gateways: dict[str, ToolGateway] = {}
        self._runtime_lock = asyncio.Lock()
        self._closed = False

    def register_tool_gateway(self, fingerprint: str, gateway: ToolGateway) -> None:
        current = self._gateways.get(fingerprint)
        if current is not None and current is not gateway:
            raise ValueError("an Agent fingerprint cannot own multiple Tool gateways")
        self._gateways[fingerprint] = gateway

    async def run_runtime(
        self,
        spec: RuntimeSpec,
        input: DshInput,
        *,
        session_id: str,
        run_id: str,
        on_event: DshEventHandler | None = None,
    ) -> DshRunResult:
        entry = await self._runtime_for(spec)
        gateway = self._gateways.get(spec.fingerprint)
        try:
            async with entry.lock:
                await self._start_dependencies(spec, entry)
                if gateway is not None:
                    gateway.bind_run(run_id)
                try:
                    return await entry.runtime.run(
                        input,
                        session_id=session_id,
                        on_event=on_event,
                    )
                finally:
                    if gateway is not None:
                        gateway.release_run(run_id)
        except BaseException as exc:
            # An interrupted SDK call may leave its synchronous worker thread
            # inside session/prompt. Evict and close the process before this
            # fingerprint is admitted for another Run.
            async with self._runtime_lock:
                if self._runtimes.get(spec.fingerprint) is entry:
                    self._runtimes.pop(spec.fingerprint)
            try:
                await entry.runtime.cancel(session_id)
            except BaseException as cancel_error:
                exc.add_note(
                    "DSH Runtime cleanup also failed: "
                    f"{type(cancel_error).__name__}: {cancel_error}"
                )
            raise

    async def prepare_runtime(self, spec: RuntimeSpec) -> None:
        entry = await self._runtime_for(spec)
        async with entry.lock:
            await self._start_dependencies(spec, entry)

    async def cancel_runtime(self, spec: RuntimeSpec, *, session_id: str) -> None:
        """Cancel the active DSH interval and evict the process at this SDK pin."""

        async with self._runtime_lock:
            entry = self._runtimes.pop(spec.fingerprint, None)
        if entry is None:
            return
        async with entry.lock:
            await entry.runtime.cancel(session_id)

    async def close(self) -> None:
        async with self._runtime_lock:
            if self._closed:
                return
            self._closed = True
            entries = tuple(self._runtimes.values())
            gateways = tuple(self._gateways.values())
            self._runtimes.clear()
            self._gateways.clear()
        failures = list(
            await asyncio.gather(
                *(entry.runtime.close() for entry in entries),
                return_exceptions=True,
            )
        )
        # Let DSH disconnect its MCP clients before stopping their loopback
        # servers. Gateway lifecycles still close even if a Runtime failed.
        failures.extend(
            await asyncio.gather(
                *(gateway.close() for gateway in gateways),
                return_exceptions=True,
            )
        )
        errors = [failure for failure in failures if isinstance(failure, BaseException)]
        if errors:
            raise BaseExceptionGroup("one or more Base Agent services failed to close", errors)

    async def __aenter__(self) -> BaseAgentApp:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        await self.close()

    async def _runtime_for(self, spec: RuntimeSpec) -> _RuntimeEntry:
        async with self._runtime_lock:
            if self._closed:
                raise RuntimeError("BaseAgentApp is closed")
            entry = self._runtimes.get(spec.fingerprint)
            if entry is None:
                entry = _RuntimeEntry(
                    self._runtime_factory.create(
                        spec,
                        dsh_home=self.state.runtime_home(spec.fingerprint),
                    )
                )
                self._runtimes[spec.fingerprint] = entry
            return entry

    async def _start_dependencies(self, spec: RuntimeSpec, entry: _RuntimeEntry) -> None:
        gateway = self._gateways.get(spec.fingerprint)
        if gateway is not None:
            await gateway.start()
        if entry.started:
            return
        stream_id = f"runtime-{spec.fingerprint}"
        await self.event_store.emit(
            stream_id,
            "runtime.starting",
            {"agent_id": spec.agent_name, "agent_fingerprint": spec.fingerprint},
        )
        try:
            await entry.runtime.start()
        except BaseException as exc:
            await self.event_store.emit(
                stream_id,
                "runtime.crashed",
                {"error_type": type(exc).__name__},
            )
            raise
        entry.started = True
        await self.event_store.emit(
            stream_id,
            "runtime.started",
            {"agent_id": spec.agent_name, "agent_fingerprint": spec.fingerprint},
        )


__all__ = ["BaseAgentApp"]
