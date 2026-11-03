"""Scripted DSH Runtime test double at the official SDK adapter boundary."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dsh_base_agent._dsh import (
    DshEventHandler,
    DshInput,
    DshRunResult,
    DshRuntime,
    RuntimeSpec,
)


@dataclass(frozen=True, slots=True)
class RecordedRuntimeCall:
    agent_name: str
    session_id: str
    prompt: str


class ScriptedRuntimeFactory:
    """Return configured responses and record the Session boundary of every call."""

    def __init__(self, responses: Mapping[str, Sequence[str | BaseException]]) -> None:
        self._responses = {name: deque(items) for name, items in responses.items()}
        self.calls: list[RecordedRuntimeCall] = []
        self.started_agents: list[str] = []
        self._lock = asyncio.Lock()

    def create(self, spec: RuntimeSpec, *, dsh_home: Path) -> DshRuntime:
        dsh_home.mkdir(parents=True, exist_ok=True)
        return _ScriptedRuntime(self, spec)

    async def _run(
        self,
        spec: RuntimeSpec,
        input: DshInput,
        session_id: str,
        on_event: DshEventHandler | None,
    ) -> DshRunResult:
        prompt = input if isinstance(input, str) else str(input)
        async with self._lock:
            self.calls.append(
                RecordedRuntimeCall(
                    agent_name=spec.agent_name,
                    session_id=session_id,
                    prompt=prompt,
                )
            )
            queue = self._responses.get(spec.agent_name)
            if queue is None or not queue:
                raise RuntimeError(f"no scripted response remains for Agent '{spec.agent_name}'")
            response = queue.popleft()
        if isinstance(response, BaseException):
            raise response
        events = (
            {"sequence": 1, "type": "turn/start", "data": {"turn": 1}},
            {
                "sequence": 2,
                "type": "step/start",
                "data": {"turn": 1, "step": 1},
            },
            {
                "sequence": 3,
                "type": "assistant/message",
                "data": {"message": {"content": [{"type": "text", "text": response}]}},
            },
            {
                "sequence": 4,
                "type": "step/end",
                "data": {"turn": 1, "step": 1},
            },
            {
                "sequence": 5,
                "type": "turn/end",
                "data": {"turn": 1, "reason": {"kind": "completed"}},
            },
        )
        if on_event is not None:
            for event in events:
                await on_event(event)
        return DshRunResult(
            session_id=session_id,
            final_response=response,
            finish_reason="completed",
            events=events,
        )

    async def _start(self, spec: RuntimeSpec) -> None:
        async with self._lock:
            if spec.agent_name not in self.started_agents:
                self.started_agents.append(spec.agent_name)


class _ScriptedRuntime:
    def __init__(self, factory: ScriptedRuntimeFactory, spec: RuntimeSpec) -> None:
        self._factory = factory
        self._spec = spec
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("scripted Runtime is closed")
        await self._factory._start(self._spec)

    async def run(
        self,
        input: DshInput,
        *,
        session_id: str,
        on_event: DshEventHandler | None = None,
    ) -> DshRunResult:
        if self._closed:
            raise RuntimeError("scripted Runtime is closed")
        return await self._factory._run(self._spec, input, session_id, on_event)

    async def cancel(self, session_id: str) -> None:
        del session_id
        self._closed = True

    async def close(self) -> None:
        self._closed = True
