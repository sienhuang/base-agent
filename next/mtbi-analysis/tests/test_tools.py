from pathlib import Path

import pytest
from base_agent.tools.core import ToolContext

from mtbi_analysis.runner import CommandResult
from mtbi_analysis.tools import build_mtbi_toolkit
from mtbi_analysis.workspace import ArtifactWorkspace


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def readiness(self) -> tuple[bool, str]:
        return True, ""

    async def run(self, args: list[str], *, cwd: Path) -> CommandResult:
        self.calls.append(args)
        return CommandResult(returncode=0, stdout="ok", stderr="")


class ExpiredAuthRunner(RecordingRunner):
    async def run(self, args: list[str], *, cwd: Path) -> CommandResult:
        self.calls.append(args)
        if args == ["auth", "status"]:
            return CommandResult(
                returncode=0,
                stdout=(
                    "已保存 token：\n  项目 12      已过期"
                ),
                stderr="",
            )
        return CommandResult(returncode=0, stdout="ok", stderr="")


def _tool_context() -> ToolContext:
    return ToolContext(run_id="run-1", agent_id="agent-1", tool_call_id="call-1")


async def test_mtbi_cli_rejects_configuration_mutation(tmp_path: Path) -> None:
    runner = RecordingRunner()
    workspace = ArtifactWorkspace(
        tmp_path,
        max_artifact_bytes=1024,
        max_csv_rows=10,
    )
    toolkit = build_mtbi_toolkit(workspace, runner)
    mtbi_cli = next(tool for tool in toolkit.tools if tool.spec.name == "mtbi_cli")

    with pytest.raises(ValueError, match="subcommand"):
        await mtbi_cli.invoke(
            {"args": ["config", "set", "onesql.endpoint", "unsafe"]},
            _tool_context(),
        )
    assert runner.calls == []


async def test_preflight_rejects_expired_auth_even_with_zero_exit_code(
    tmp_path: Path,
) -> None:
    runner = ExpiredAuthRunner()
    workspace = ArtifactWorkspace(
        tmp_path,
        max_artifact_bytes=1024,
        max_csv_rows=10,
    )
    toolkit = build_mtbi_toolkit(workspace, runner)
    preflight = next(
        tool for tool in toolkit.tools if tool.spec.name == "mtbi_preflight"
    )

    with pytest.raises(RuntimeError, match="auth login --project 12"):
        await preflight.invoke({}, _tool_context())


async def test_mtbi_cli_rejects_output_outside_workspace(tmp_path: Path) -> None:
    runner = RecordingRunner()
    workspace = ArtifactWorkspace(
        tmp_path,
        max_artifact_bytes=1024,
        max_csv_rows=10,
    )
    toolkit = build_mtbi_toolkit(workspace, runner)
    mtbi_cli = next(tool for tool in toolkit.tools if tool.spec.name == "mtbi_cli")

    with pytest.raises(ValueError, match="relative"):
        await mtbi_cli.invoke(
            {
                "args": [
                    "onesql",
                    "SELECT 1",
                    "--output",
                    "/tmp/outside.csv",
                ]
            },
            _tool_context(),
        )
    assert runner.calls == []

    with pytest.raises(ValueError, match="relative"):
        await mtbi_cli.invoke(
            {
                "args": [
                    "onesql",
                    "SELECT 1",
                    "--output=/tmp/outside.csv",
                ]
            },
            _tool_context(),
        )
    assert runner.calls == []
