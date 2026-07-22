"""Small Sandbox session port implemented by isolated backends."""

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from base_agent.sandbox.models import SandboxCommandResult, SandboxFileContent


@runtime_checkable
class SandboxSession(Protocol):
    async def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
    ) -> SandboxCommandResult: ...

    async def read_text(self, path: str) -> SandboxFileContent: ...

    async def write_text(self, path: str, content: str, *, append: bool = False) -> None: ...
