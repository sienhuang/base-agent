"""Application-owned, workspace-bounded Tools."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from base_agent import (
    ArtifactManager,
    SideEffect,
    ToolContext,
    Toolkit,
    ToolReadiness,
    tool,
)

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
    async def mtbi_preflight(context: ToolContext) -> dict[str, Any]:
        """Check MTBI authentication and configuration without changing them."""
        return await check_mtbi_environment(await _workspace(context), runner)

    @tool(name="mtbi_cli", readiness=readiness, timeout_seconds=900)
    async def mtbi_cli(args: list[str], context: ToolContext) -> dict[str, Any]:
        """Run an allowlisted, shell-free MTBI metadata or OneSQL command."""
        runtime_workspace = await _workspace(context)
        normalized = _validate_mtbi_args(args, runtime_workspace)
        result = await runner.run(normalized, cwd=runtime_workspace.root)
        if result.returncode != 0:
            detail = result.stderr or result.stdout or f"exit code {result.returncode}"
            raise RuntimeError(f"mtbi-cli failed: {detail}")
        response: dict[str, Any] = {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        output = _output_path(normalized)
        if output is not None:
            artifacts = _artifacts(context)
            content = runtime_workspace.read_bytes(output)
            artifact = await artifacts.create(
                name=_artifact_name(output),
                media_type=_media_type(output),
                content=content,
                location=output,
                max_bytes=runtime_workspace.max_artifact_bytes,
            )
            response["artifact_id"] = artifact.id
            response["sha256"] = artifact.sha256
        return response

    @tool(
        name="write_workspace_text",
        permissions=frozenset({"workspace:write"}),
        side_effect=SideEffect.IDEMPOTENT,
    )
    async def write_workspace_text(
        path: str,
        content: str,
        context: ToolContext,
    ) -> dict[str, Any]:
        """Atomically write bounded UTF-8 text inside the configured workspace."""
        runtime_workspace = await _workspace(context)
        result = runtime_workspace.write_text(path, content)
        artifacts = _artifacts(context)
        sources: tuple[str, ...] = ()
        if _artifact_name(path) == "analysis_report":
            try:
                sources = ((await artifacts.find("query_dataset")).id,)
            except LookupError:
                sources = ()
        artifact = await artifacts.create(
            name=_artifact_name(path),
            media_type=_media_type(path),
            content=content,
            location=str(result["path"]),
            source_artifact_ids=sources,
            max_bytes=runtime_workspace.max_artifact_bytes,
        )
        return {**result, "artifact_id": artifact.id}

    @tool(name="summarize_csv")
    async def summarize_csv(path: str, context: ToolContext) -> dict[str, Any]:
        """Read a bounded workspace CSV and return rows, samples, and numeric summaries."""
        runtime_workspace = await _workspace(context)
        visible_dataset = await _artifacts(context).find("query_dataset")
        if visible_dataset.location != path:
            raise ValueError(
                "requested CSV path does not match the granted query_dataset Artifact"
            )
        raw = runtime_workspace.read_bytes(path)
        if visible_dataset.sha256 != hashlib.sha256(raw).hexdigest():
            raise ValueError("workspace CSV no longer matches the granted Artifact")
        return runtime_workspace.summarize_csv(path)

    return Toolkit.of(
        "mtbi-analysis",
        mtbi_preflight,
        mtbi_cli,
        write_workspace_text,
        summarize_csv,
    )


async def _workspace(context: ToolContext) -> ArtifactWorkspace:
    if context.resources is None:
        raise RuntimeError("ToolContext has no ResourceManager")
    return await context.resources.get("workspace", ArtifactWorkspace)


def _artifacts(context: ToolContext) -> ArtifactManager:
    if context.artifacts is None:
        raise RuntimeError("ToolContext has no ArtifactManager")
    return context.artifacts


def _output_path(args: list[str]) -> str | None:
    for index, value in enumerate(args):
        if value == "--output" and index + 1 < len(args):
            return args[index + 1]
        if value.startswith("--output="):
            return value.split("=", 1)[1]
    return None


def _artifact_name(path: str) -> str:
    return {
        "query.sql": "query_sql",
        "dataset.csv": "query_dataset",
        "report.md": "analysis_report",
    }.get(Path(path).name, Path(path).stem.replace("-", "_"))


def _media_type(path: str) -> str:
    return {
        ".sql": "application/sql",
        ".csv": "text/csv",
        ".md": "text/markdown",
    }.get(Path(path).suffix.lower(), "text/plain")


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
