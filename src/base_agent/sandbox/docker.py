"""Hardened, execution-scoped Docker Sandbox implementation."""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import Any
from uuid import uuid4

import docker  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.resources import ResourceSpec
from base_agent.runtime.context import RuntimeContext
from base_agent.sandbox.models import SandboxCommandResult, SandboxFileContent


class SandboxClosedError(RuntimeError):
    """The backing container is no longer available."""


class SandboxPathError(ValueError):
    """A requested path escaped the configured workspace."""


class SandboxFileError(RuntimeError):
    """A Sandbox file operation failed."""


class DockerSandboxConfig(BaseModel):
    """Explicit limits for one disposable Docker Sandbox."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str = Field(min_length=1)
    working_dir: str = "/workspace"
    user: str = "65534:65534"
    network_enabled: bool = False
    read_only_root: bool = True
    memory_limit: str | int = "512m"
    nano_cpus: int = Field(default=1_000_000_000, ge=1)
    pids_limit: int = Field(default=128, ge=1)
    workspace_size_mb: int = Field(default=64, ge=1, le=4096)
    command_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    max_output_bytes: int = Field(default=1_000_000, ge=1024, le=20_000_000)
    max_file_bytes: int = Field(default=1_000_000, ge=1, le=20_000_000)

    @model_validator(mode="after")
    def validate_container_boundary(self) -> DockerSandboxConfig:
        workspace = PurePosixPath(self.working_dir)
        if not workspace.is_absolute() or str(workspace) == "/" or ".." in workspace.parts:
            raise ValueError("working_dir must be an absolute non-root container path")
        if not self.image.strip():
            raise ValueError("image must not be blank")
        if not self.user.strip():
            raise ValueError("user must not be blank")
        return self


class DockerSandboxSession:
    """One disposable container with a bounded writable tmpfs workspace."""

    def __init__(self, client: Any, container: Any, config: DockerSandboxConfig) -> None:
        self._client = client
        self._container = container
        self.config = config
        self._closed = False
        self._lock = asyncio.Lock()

    @classmethod
    async def create(cls, config: DockerSandboxConfig) -> DockerSandboxSession:
        client = await asyncio.to_thread(docker.from_env)
        name = f"base-agent-sandbox-{uuid4().hex[:12]}"
        tmpfs_options = (
            f"rw,noexec,nosuid,nodev,size={config.workspace_size_mb}m,mode=1777"
        )
        try:
            await asyncio.to_thread(client.images.get, config.image)
            container = await asyncio.to_thread(
                client.containers.run,
                config.image,
                ["-c", "while :; do sleep 3600; done"],
                entrypoint="/bin/sh",
                name=name,
                detach=True,
                auto_remove=False,
                network_mode="bridge" if config.network_enabled else "none",
                read_only=config.read_only_root,
                user=config.user,
                working_dir=config.working_dir,
                mem_limit=config.memory_limit,
                nano_cpus=config.nano_cpus,
                pids_limit=config.pids_limit,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges"],
                privileged=False,
                tmpfs={config.working_dir: tmpfs_options, "/tmp": tmpfs_options},
                labels={"base-agent.component": "sandbox"},
            )
        except BaseException:
            await asyncio.to_thread(client.close)
            raise
        return cls(client, container, config)

    @property
    def id(self) -> str:
        return str(self._container.id)

    async def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
    ) -> SandboxCommandResult:
        self._ensure_open()
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("argv must contain one or more non-empty strings")
        resolved_cwd = self._resolve_path(cwd)
        started = time.monotonic()
        try:
            async with self._lock:
                async with asyncio.timeout(self.config.command_timeout_seconds):
                    exit_code, stdout, stderr, truncated = await asyncio.to_thread(
                        self._execute_sync,
                        tuple(argv),
                        resolved_cwd,
                        dict(environment or {}),
                    )
        except TimeoutError:
            await self.close()
            raise
        except asyncio.CancelledError:
            await self.close()
            raise
        return SandboxCommandResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            truncated=truncated,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    async def read_text(self, path: str) -> SandboxFileContent:
        resolved = self._resolve_path(path)
        result = await self.execute(["/bin/cat", resolved])
        if result.exit_code != 0:
            raise SandboxFileError(result.stderr or f"could not read '{path}'")
        return SandboxFileContent(
            path=path,
            content=result.stdout,
            truncated=result.truncated,
        )

    async def write_text(self, path: str, content: str, *, append: bool = False) -> None:
        self._ensure_open()
        resolved = PurePosixPath(self._resolve_path(path))
        payload = content.encode("utf-8")
        if append:
            try:
                existing = await self.read_text(path)
            except SandboxFileError:
                existing = None
            if existing is not None:
                if existing.truncated:
                    raise SandboxFileError("cannot append to a file larger than the read limit")
                payload = existing.content.encode("utf-8") + payload
        if len(payload) > self.config.max_file_bytes:
            raise SandboxFileError(
                f"file exceeds max_file_bytes={self.config.max_file_bytes}"
            )
        parent = str(resolved.parent)
        mkdir = await self.execute(["/bin/mkdir", "-p", parent])
        if mkdir.exit_code != 0:
            raise SandboxFileError(mkdir.stderr or f"could not create '{parent}'")
        written = await self.execute(
            [
                "/bin/sh",
                "-c",
                'printf "%s" "$BASE_AGENT_CONTENT" | base64 -d > "$BASE_AGENT_PATH"',
            ],
            environment={
                "BASE_AGENT_CONTENT": base64.b64encode(payload).decode("ascii"),
                "BASE_AGENT_PATH": str(resolved),
            },
        )
        if written.exit_code != 0:
            raise SandboxFileError(written.stderr or f"could not write '{path}'")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self._container.remove, force=True)
        finally:
            await asyncio.to_thread(self._client.close)

    def _execute_sync(
        self,
        argv: tuple[str, ...],
        cwd: str,
        environment: dict[str, str],
    ) -> tuple[int, str, str, bool]:
        created = self._client.api.exec_create(
            self._container.id,
            cmd=list(argv),
            workdir=cwd,
            environment=environment,
            stdout=True,
            stderr=True,
            stdin=False,
            tty=False,
        )
        exec_id = created["Id"]
        stream = self._client.api.exec_start(exec_id, stream=True, demux=True)
        stdout = bytearray()
        stderr = bytearray()
        truncated = False
        try:
            for out_chunk, err_chunk in stream:
                available = max(0, self.config.max_output_bytes - len(stdout) - len(stderr))
                truncated |= _append_available(stdout, out_chunk, available)
                available = max(0, self.config.max_output_bytes - len(stdout) - len(stderr))
                truncated |= _append_available(stderr, err_chunk, available)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    close()
                except OSError:
                    response = getattr(stream, "_response", None)
                    if response is not None:
                        response.close()
        inspected = self._client.api.exec_inspect(exec_id)
        return (
            int(inspected["ExitCode"]),
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            truncated,
        )

    def _resolve_path(self, path: str) -> str:
        candidate = PurePosixPath(path)
        workspace = PurePosixPath(self.config.working_dir)
        if candidate.is_absolute():
            try:
                relative = candidate.relative_to(workspace)
            except ValueError as exc:
                raise SandboxPathError("path must stay inside the Sandbox workspace") from exc
        else:
            relative = candidate
        if ".." in relative.parts:
            raise SandboxPathError("path must stay inside the Sandbox workspace")
        return str(workspace.joinpath(relative))

    def _ensure_open(self) -> None:
        if self._closed:
            raise SandboxClosedError("Sandbox session is closed")


def docker_sandbox_resource(
    config: DockerSandboxConfig,
    *,
    name: str = "sandbox",
    eager: bool = False,
) -> ResourceSpec:
    """Create an execution-scoped ResourceSpec for a disposable Docker Sandbox."""

    @asynccontextmanager
    async def factory(context: RuntimeContext) -> AsyncIterator[DockerSandboxSession]:
        del context
        session = await DockerSandboxSession.create(config)
        try:
            yield session
        finally:
            await session.close()

    return ResourceSpec(name=name, factory=factory, eager=eager)


def _append_available(target: bytearray, chunk: bytes | None, available: int) -> bool:
    if not chunk:
        return False
    target.extend(chunk[:available])
    return len(chunk) > available
