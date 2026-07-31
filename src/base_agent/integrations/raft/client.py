"""Async, shell-free client for the published Raft Agent CLI."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import deque
from typing import Protocol

from base_agent.integrations.raft.errors import (
    RaftBridgeExitedError,
    RaftCliCommandError,
    RaftCliNotFoundError,
    RaftCliOutputLimitError,
    RaftCliTimeoutError,
)
from base_agent.integrations.raft.models import (
    RaftInboxBatch,
    RaftWorkerConfig,
)
from base_agent.integrations.raft.parser import parse_raft_messages

logger = logging.getLogger(__name__)


class RaftClient(Protocol):
    """Transport surface consumed by `RaftWorker` and replaceable in tests."""

    async def check_messages(self) -> RaftInboxBatch: ...

    async def send_message(self, target: str, content: str) -> None: ...

    async def claim_task(self, target: str, task_number: int) -> None: ...

    async def update_task(
        self,
        target: str,
        task_number: int,
        status: str,
    ) -> None: ...

    async def start_bridge(
        self,
        *,
        wake_endpoint: str,
        wake_token: str,
        runtime_session: str,
    ) -> RaftBridge: ...


class RaftBridge:
    """A supervised `raft agent bridge` subprocess."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        stdout_task: asyncio.Task[tuple[str, ...]],
        stderr_task: asyncio.Task[tuple[str, ...]],
    ) -> None:
        self._process = process
        self._stdout_task = stdout_task
        self._stderr_task = stderr_task
        self._closing = False

    @property
    def pid(self) -> int | None:
        return self._process.pid

    async def wait(self) -> None:
        returncode = await self._process.wait()
        stdout_lines, stderr_lines = await asyncio.gather(
            self._stdout_task,
            self._stderr_task,
        )
        if self._closing:
            return
        detail = "\n".join(stderr_lines[-10:] or stdout_lines[-5:])[:2_000]
        raise RaftBridgeExitedError(
            f"raft wake bridge exited unexpectedly with code {returncode}: "
            f"{detail or 'no diagnostic output'}"
        )

    async def close(self) -> None:
        self._closing = True
        if self._process.returncode is None:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except TimeoutError:
                self._process.kill()
                await self._process.wait()
        await asyncio.gather(
            self._stdout_task,
            self._stderr_task,
            return_exceptions=True,
        )


class RaftCliClient:
    """Invoke Raft through argv and stdin without giving a shell to the model."""

    def __init__(self, config: RaftWorkerConfig) -> None:
        self.config = config

    async def check_messages(self) -> RaftInboxBatch:
        raw = await self._run("message check", "message", "check")
        return RaftInboxBatch(raw=raw, messages=parse_raft_messages(raw))

    async def send_message(self, target: str, content: str) -> None:
        await self._run(
            "message send",
            "message",
            "send",
            "--target",
            target,
            stdin=content,
        )

    async def claim_task(self, target: str, task_number: int) -> None:
        await self._run(
            "task claim",
            "task",
            "claim",
            "--target",
            target,
            "--number",
            str(task_number),
        )

    async def update_task(
        self,
        target: str,
        task_number: int,
        status: str,
    ) -> None:
        await self._run(
            "task update",
            "task",
            "update",
            "--target",
            target,
            "--number",
            str(task_number),
            "--status",
            status,
        )

    async def start_bridge(
        self,
        *,
        wake_endpoint: str,
        wake_token: str,
        runtime_session: str,
    ) -> RaftBridge:
        state_dir = self.config.profile_state_dir / "bridge"
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        arguments = (
            self.config.executable,
            "--profile",
            self.config.profile,
            "agent",
            "bridge",
            "--json",
            "--adapter-instance",
            self.config.adapter_instance,
            "--poll-interval-ms",
            str(self.config.bridge_poll_interval_ms),
            "--state-dir",
            str(state_dir),
            "--wake-adapter",
            "wake-channel",
            "--wake-channel-endpoint",
            wake_endpoint,
            "--runtime-session",
            runtime_session,
        )
        environment = self._environment()
        environment["RAFT_CHANNEL_TOKEN"] = wake_token
        try:
            process = await asyncio.create_subprocess_exec(
                *arguments,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
        except FileNotFoundError as exc:
            raise RaftCliNotFoundError(
                f"Raft CLI executable '{self.config.executable}' was not found"
            ) from exc
        stdout_task = asyncio.create_task(
            _capture_lines(process.stdout, log_level=logging.DEBUG)
        )
        stderr_task = asyncio.create_task(
            _capture_lines(process.stderr, log_level=logging.WARNING)
        )
        return RaftBridge(
            process,
            stdout_task=stdout_task,
            stderr_task=stderr_task,
        )

    async def _run(
        self,
        command_name: str,
        *arguments: str,
        stdin: str | None = None,
    ) -> str:
        argv = (
            self.config.executable,
            "--profile",
            self.config.profile,
            *arguments,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=(
                    asyncio.subprocess.PIPE
                    if stdin is not None
                    else asyncio.subprocess.DEVNULL
                ),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._environment(),
            )
        except FileNotFoundError as exc:
            raise RaftCliNotFoundError(
                f"Raft CLI executable '{self.config.executable}' was not found"
            ) from exc

        if stdin is not None:
            assert process.stdin is not None
            try:
                process.stdin.write(stdin.encode("utf-8"))
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        stdout_task = asyncio.create_task(
            _read_limited(process.stdout, self.config.cli_max_output_bytes)
        )
        stderr_task = asyncio.create_task(
            _read_limited(process.stderr, self.config.cli_max_output_bytes)
        )
        wait_task = asyncio.create_task(process.wait())
        try:
            returncode, stdout, stderr = await asyncio.wait_for(
                asyncio.gather(wait_task, stdout_task, stderr_task),
                timeout=self.config.cli_timeout_seconds,
            )
        except TimeoutError as exc:
            await _stop_process(process)
            _cancel_tasks(wait_task, stdout_task, stderr_task)
            raise RaftCliTimeoutError(
                f"raft CLI command '{command_name}' timed out after "
                f"{self.config.cli_timeout_seconds:g}s"
            ) from exc
        except RaftCliOutputLimitError:
            await _stop_process(process)
            _cancel_tasks(wait_task, stdout_task, stderr_task)
            raise

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        if returncode != 0:
            raise RaftCliCommandError(command_name, returncode, stderr_text)
        return stdout_text

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        cli_state = self.config.profile_state_dir / "cli-state"
        cli_state.mkdir(parents=True, exist_ok=True, mode=0o700)
        environment["SLOCK_CLI_CONSUMED_SEQ_STATE_DIR"] = str(cli_state)
        return environment


async def _read_limited(
    stream: asyncio.StreamReader | None,
    limit: int,
) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise RaftCliOutputLimitError(
                f"raft CLI output exceeded the configured {limit}-byte limit"
            )
        chunks.append(chunk)


async def _capture_lines(
    stream: asyncio.StreamReader | None,
    *,
    log_level: int,
) -> tuple[str, ...]:
    if stream is None:
        return ()
    recent: deque[str] = deque(maxlen=100)
    while True:
        line = await stream.readline()
        if not line:
            return tuple(recent)
        text = line.decode("utf-8", errors="replace").rstrip()[:4_096]
        recent.append(text)
        logger.log(
            log_level,
            "raft bridge output",
            extra={"event": "raft.bridge.output", "bridge_line": text},
        )


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        process.kill()
        await process.wait()


def _cancel_tasks(*tasks: asyncio.Task[object]) -> None:
    for task in tasks:
        if not task.done():
            task.cancel()
