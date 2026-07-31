from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    ModelResponse,
    ResourceSpec,
    ToolCall,
    coding_bundle,
)
from base_agent.sandbox import SandboxCommandResult, SandboxFileContent, SandboxSession
from base_agent.testing import FakeModel


class FakeCodingSandbox:
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
        return SandboxCommandResult(
            exit_code=0,
            stdout="42\n",
            duration_ms=1,
        )

    async def read_text(self, path: str) -> SandboxFileContent:
        return SandboxFileContent(path=path, content=self.files[path])

    async def write_text(
        self,
        path: str,
        content: str,
        *,
        append: bool = False,
    ) -> None:
        self.files[path] = self.files.get(path, "") + content if append else content


@pytest.mark.asyncio
async def test_coding_bundle_composes_one_shared_sandbox_workspace() -> None:
    sandbox = FakeCodingSandbox()

    @asynccontextmanager
    async def sandbox_resource(context: Any) -> AsyncIterator[FakeCodingSandbox]:
        del context
        yield sandbox

    bundle = coding_bundle(ResourceSpec("analysis-sandbox", sandbox_resource))
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="write-code",
                        name="sandbox_write_text",
                        arguments={
                            "path": "analysis.py",
                            "content": "print(6 * 7)\n",
                        },
                    ),
                    ToolCall(
                        id="run-code",
                        name="sandbox_execute",
                        arguments={"argv": ["python", "analysis.py"]},
                    ),
                )
            ),
            ModelResponse(content="The result is 42."),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="coding-bundle",
            instructions="Write and execute code in the isolated workspace.",
            tools=bundle.tool_names,
            permissions=bundle.required_permissions,
        ),
        model=model,
        tools=bundle.tools,
        resources=bundle.resources,
    )

    result = await agent.run("Calculate six times seven with Python.")

    assert isinstance(sandbox, SandboxSession)
    assert result.status is AgentResultStatus.COMPLETED
    assert sandbox.files == {"analysis.py": "print(6 * 7)\n"}
    assert sandbox.executions == [(("python", "analysis.py"), ".")]
    assert bundle.required_permissions == frozenset(
        {"sandbox:read", "sandbox:write", "sandbox:execute"}
    )


def test_coding_bundle_actions_are_explicit() -> None:
    @asynccontextmanager
    async def unused_resource(context: Any) -> AsyncIterator[FakeCodingSandbox]:
        del context
        yield FakeCodingSandbox()

    bundle = coding_bundle(
        ResourceSpec("read-only-sandbox", unused_resource),
        allow_write=False,
        allow_execute=False,
    )

    assert bundle.tool_names == ("sandbox_read_text",)
    assert bundle.required_permissions == frozenset({"sandbox:read"})

    with pytest.raises(ValueError, match="at least one action"):
        coding_bundle(
            ResourceSpec("disabled-sandbox", unused_resource),
            allow_read=False,
            allow_write=False,
            allow_execute=False,
        )
