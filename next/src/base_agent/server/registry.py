"""Process-local retention for background Run handles."""

from __future__ import annotations

from base_agent.agent import Agent
from base_agent.run_handle import RunHandle


class RunNotFoundError(LookupError):
    """The requested process-local Run is unknown."""


class InMemoryRunRegistry:
    """Retain RunHandles for HTTP status polling in one server process."""

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._handles: dict[str, RunHandle] = {}

    def start(self, prompt: str, *, stream: bool = False) -> RunHandle:
        handle = self._agent.start(prompt, stream=stream)
        self._handles[handle.run_id] = handle
        return handle

    def get(self, run_id: str) -> RunHandle:
        try:
            return self._handles[run_id]
        except KeyError as exc:
            raise RunNotFoundError(f"Run '{run_id}' was not found") from exc
