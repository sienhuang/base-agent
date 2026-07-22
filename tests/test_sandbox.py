import asyncio
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import docker
import pytest
from docker.errors import NotFound
from pydantic import ValidationError

from base_agent import Agent, AgentProfile, AgentResultStatus, ModelResponse, ResourceSpec, ToolCall
from base_agent.sandbox import (
    SandboxCommandResult,
    SandboxFileContent,
    SandboxSession,
    sandbox_tools,
)
from base_agent.sandbox.docker import (
    DockerSandboxConfig,
    DockerSandboxSession,
    SandboxClosedError,
    SandboxPathError,
)
from base_agent.testing import FakeModel

SANDBOX_IMAGE = os.getenv("BASE_AGENT_TEST_SANDBOX_IMAGE")
requires_docker_sandbox = pytest.mark.skipif(
    SANDBOX_IMAGE is None,
    reason="set BASE_AGENT_TEST_SANDBOX_IMAGE to run Docker Sandbox integration tests",
)


class FakeSandbox:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.executions: list[tuple[tuple[str, ...], str]] = []

    async def execute(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        environment: Mapping[str, str] | None = None,
    ) -> SandboxCommandResult:
        del environment
        self.executions.append((tuple(argv), cwd))
        return SandboxCommandResult(exit_code=0, stdout="executed", duration_ms=1)

    async def read_text(self, path: str) -> SandboxFileContent:
        return SandboxFileContent(path=path, content=self.files[path])

    async def write_text(self, path: str, content: str, *, append: bool = False) -> None:
        self.files[path] = self.files.get(path, "") + content if append else content


def test_docker_sandbox_configuration_rejects_unsafe_workspace() -> None:
    with pytest.raises(ValidationError, match="absolute non-root"):
        DockerSandboxConfig(image="example", working_dir="/")
    with pytest.raises(ValidationError, match="absolute non-root"):
        DockerSandboxConfig(image="example", working_dir="relative")


@pytest.mark.asyncio
async def test_generic_sandbox_tools_use_permissions_and_resource_scope() -> None:
    sandbox = FakeSandbox()

    @asynccontextmanager
    async def resource(context: Any) -> AsyncIterator[FakeSandbox]:
        del context
        yield sandbox

    tools = sandbox_tools()
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write",
                        name="sandbox_write_text",
                        arguments={"path": "note.txt", "content": "hello"},
                    ),
                    ToolCall(
                        id="execute",
                        name="sandbox_execute",
                        arguments={"argv": ["wc", "-c", "note.txt"]},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="sandbox-agent",
            instructions="Use the isolated workspace.",
            tools=("sandbox_write_text", "sandbox_execute"),
            permissions=frozenset({"sandbox:write", "sandbox:execute"}),
        ),
        model=model,
        tools=tools,
        resources=(ResourceSpec("sandbox", resource),),
    )

    result = await agent.run("write and inspect")

    assert isinstance(sandbox, SandboxSession)
    assert result.status is AgentResultStatus.COMPLETED
    assert sandbox.files == {"note.txt": "hello"}
    assert sandbox.executions == [(('wc', '-c', 'note.txt'), ".")]


@requires_docker_sandbox
@pytest.mark.asyncio
async def test_real_docker_sandbox_is_hardened_bounded_and_disposable() -> None:
    assert SANDBOX_IMAGE is not None
    session = await DockerSandboxSession.create(
        DockerSandboxConfig(
            image=SANDBOX_IMAGE,
            command_timeout_seconds=5,
            max_output_bytes=1024,
        )
    )
    container_id = session.id
    inspection_client = docker.from_env()
    try:
        inspection = inspection_client.api.inspect_container(container_id)
        host = inspection["HostConfig"]
        assert host["NetworkMode"] == "none"
        assert host["ReadonlyRootfs"] is True
        assert "ALL" in host["CapDrop"]
        assert "no-new-privileges" in host["SecurityOpt"]
        assert host["PidsLimit"] == 128

        result = await session.execute(
            ["/bin/sh", "-c", "printf output; printf error >&2"]
        )
        assert result.exit_code == 0
        assert result.stdout == "output"
        assert result.stderr == "error"

        await session.write_text("reports/result.txt", "first")
        await session.write_text("reports/result.txt", "+second", append=True)
        assert (await session.read_text("reports/result.txt")).content == "first+second"
        with pytest.raises(SandboxPathError):
            await session.read_text("../etc/passwd")

        bounded = await session.execute(
            ["/bin/sh", "-c", "head -c 2048 /dev/zero | tr '\\0' x"]
        )
        assert bounded.truncated is True
        assert len(bounded.stdout.encode()) == 1024
    finally:
        await session.close()
        inspection_client.close()

    with pytest.raises(SandboxClosedError):
        await session.execute(["/bin/true"])
    verifier = docker.from_env()
    try:
        with pytest.raises(NotFound):
            verifier.containers.get(container_id)
    finally:
        verifier.close()


@requires_docker_sandbox
@pytest.mark.asyncio
async def test_docker_command_timeout_destroys_the_sandbox() -> None:
    assert SANDBOX_IMAGE is not None
    session = await DockerSandboxSession.create(
        DockerSandboxConfig(image=SANDBOX_IMAGE, command_timeout_seconds=0.05)
    )
    container_id = session.id

    with pytest.raises(TimeoutError):
        await session.execute(["/bin/sleep", "2"])

    verifier = docker.from_env()
    try:
        for _ in range(50):
            try:
                verifier.containers.get(container_id)
            except NotFound:
                break
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - diagnostic guard
            pytest.fail("timed out Sandbox container was not removed")
    finally:
        verifier.close()
