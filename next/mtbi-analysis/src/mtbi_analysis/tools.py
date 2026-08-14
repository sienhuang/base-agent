"""Application-owned, workspace-bounded Tools."""

from __future__ import annotations

import re
from typing import Any

from base_agent import SideEffect, Toolkit, ToolReadiness, tool

from mtbi_analysis.runner import MTBICommandRunner
from mtbi_analysis.workspace import ArtifactWorkspace


class MTBIEnvironmentError(RuntimeError):
    pass


async def check_mtbi_environment(
    workspace: ArtifactWorkspace,
    runner: MTBICommandRunner,
) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    failure_detail: str | None = None
    for name, args in (
        ("auth", ["auth", "status"]),
        ("config", ["config", "show"]),
    ):
        result = await runner.run(args, cwd=workspace.root)
        expired_auth = name == "auth" and _auth_expired(
            f"{result.stdout}\n{result.stderr}"
        )
        checks[name] = {
            "ok": result.returncode == 0 and not expired_auth,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        if expired_auth:
            failure_detail = _expired_auth_instruction(result.stdout)
        elif result.returncode != 0 and failure_detail is None:
            failure_detail = result.stderr or result.stdout or f"{name} failed"
    if not all(check["ok"] for check in checks.values()):
        raise MTBIEnvironmentError(
            f"MTBI preflight failed: {failure_detail or 'not ready'}"
        )
    return {"ready": True, "checks": checks}


def build_mtbi_toolkit(
    workspace: ArtifactWorkspace,
    runner: MTBICommandRunner,
) -> Toolkit:
    def readiness() -> ToolReadiness:
        ready, detail = runner.readiness()
        return ToolReadiness(ready=ready, detail=detail)

    @tool(name="mtbi_preflight", readiness=readiness, timeout_seconds=60)
    async def mtbi_preflight() -> dict[str, Any]:
        """Check MTBI authentication and configuration without changing them."""
        return await check_mtbi_environment(workspace, runner)

    @tool(name="mtbi_cli", readiness=readiness, timeout_seconds=900)
    async def mtbi_cli(args: list[str]) -> dict[str, Any]:
        """Run an allowlisted, shell-free MTBI metadata or OneSQL command."""
        normalized = _validate_mtbi_args(args, workspace)
        result = await runner.run(normalized, cwd=workspace.root)
        if result.returncode != 0:
            detail = result.stderr or result.stdout or f"exit code {result.returncode}"
            raise RuntimeError(f"mtbi-cli failed: {detail}")
        return {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    @tool(
        name="write_workspace_text",
        permissions=frozenset({"workspace:write"}),
        side_effect=SideEffect.IDEMPOTENT,
    )
    def write_workspace_text(path: str, content: str) -> dict[str, Any]:
        """Atomically write bounded UTF-8 text inside the configured workspace."""
        return workspace.write_text(path, content)

    @tool(name="summarize_csv")
    def summarize_csv(path: str) -> dict[str, Any]:
        """Read a bounded workspace CSV and return rows, samples, and numeric summaries."""
        return workspace.summarize_csv(path)

    return Toolkit.of(
        "mtbi-analysis",
        mtbi_preflight,
        mtbi_cli,
        write_workspace_text,
        summarize_csv,
    )


def _validate_mtbi_args(
    args: list[str],
    workspace: ArtifactWorkspace,
) -> list[str]:
    if not args or any(not item or "\x00" in item for item in args):
        raise ValueError("mtbi_cli requires non-empty argv values")
    command = args[0]
    allowed = {
        "help": None,
        "auth": frozenset({"status", "--help"}),
        "config": frozenset({"show", "--help"}),
        "meta": frozenset({"search", "info", "databases", "--help"}),
        "onesql": None,
    }
    if command not in allowed:
        raise ValueError(f"mtbi_cli command '{command}' is not allowed")
    subcommands = allowed[command]
    if subcommands is not None:
        if len(args) < 2 or args[1] not in subcommands:
            raise ValueError(f"mtbi_cli subcommand for '{command}' is not allowed")

    normalized = list(args)
    for index, value in enumerate(normalized[:-1]):
        if value != "--output":
            continue
        output = workspace.resolve(normalized[index + 1])
        normalized[index + 1] = workspace.relative(output)
    for index, value in enumerate(normalized):
        if not value.startswith("--output="):
            continue
        output = workspace.resolve(value.split("=", 1)[1])
        normalized[index] = f"--output={workspace.relative(output)}"
    if "--output" in normalized and normalized[-1] == "--output":
        raise ValueError("--output requires a path")
    return normalized


def _auth_expired(output: str) -> bool:
    normalized = output.lower()
    return "已过期" in output or "expired" in normalized


def _expired_auth_instruction(output: str) -> str:
    for line in output.splitlines():
        if "mtbi-cli auth login" in line:
            return line.removeprefix("提示：").strip()
    project = re.search(r"项目\s+(\d+).*已过期", output)
    if project is not None:
        return f"MTBI authentication expired; run `mtbi-cli auth login --project {project[1]}`"
    return "MTBI authentication expired; run `mtbi-cli auth login`"
