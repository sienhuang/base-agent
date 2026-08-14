"""Narrow, shell-free boundary around the real mtbi-cli executable."""

from __future__ import annotations

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class MTBICommandRunner(Protocol):
    def readiness(self) -> tuple[bool, str]: ...

    async def run(self, args: list[str], *, cwd: Path) -> CommandResult: ...


class SubprocessMTBICommandRunner:
    def __init__(
        self,
        executable: str,
        *,
        timeout_seconds: float,
        max_output_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        if not executable.strip():
            raise ValueError("MTBI executable must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("MTBI timeout must be greater than zero")
        if max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        candidate = Path(executable).expanduser()
        self.executable = (
            str(candidate.resolve())
            if candidate.is_absolute() or candidate.parent != Path(".")
            else executable
        )
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def readiness(self) -> tuple[bool, str]:
        candidate = Path(self.executable)
        if candidate.parent != Path(".") or candidate.is_absolute():
            if candidate.is_file():
                return True, ""
            return False, f"MTBI executable '{candidate}' was not found"
        resolved = shutil.which(self.executable)
        if resolved is None:
            return False, f"MTBI executable '{self.executable}' was not found on PATH"
        return True, ""

    async def run(self, args: list[str], *, cwd: Path) -> CommandResult:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.communicate()
            raise TimeoutError(
                f"mtbi-cli exceeded {self.timeout_seconds:g}s timeout"
            ) from None
        if len(stdout) + len(stderr) > self.max_output_bytes:
            raise ValueError(
                f"mtbi-cli output exceeds {self.max_output_bytes} byte limit"
            )
        return CommandResult(
            returncode=process.returncode or 0,
            stdout=_redact(stdout.decode("utf-8", errors="replace").strip()),
            stderr=_redact(stderr.decode("utf-8", errors="replace").strip()),
        )


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(token|api[_-]?key|password|secret)(\s*[:=]\s*)([^\s,}\]]+)"
)


def _redact(value: str) -> str:
    return _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", value)
