"""Governed execution adapter for selected Skill Package scripts."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from base_agent.skills.models import Skill, SkillPackage, SkillScriptRuntime
from base_agent.tools import Tool, tool


def build_skill_script_tool(skill: Skill) -> Tool | None:
    """Build one runtime-owned Tool for an opted-in Skill Package."""
    policy = skill.script_execution
    package = skill.package
    if not policy.enabled:
        return None
    if package is None:  # Skill construction validates this invariant.
        raise RuntimeError("enabled Skill script execution has no package")

    tool_name = skill_script_tool_name(skill)
    visible_scripts = ", ".join(package.script_files)

    @tool(
        name=tool_name,
        description=(
            f"Run one script from the '{skill.name}' Skill Package. "
            f"Allowed scripts: {visible_scripts}. Send the script input as "
            "the payload object."
        ),
        permissions=frozenset({policy.permission}),
        timeout_seconds=policy.timeout_seconds,
        side_effect=policy.side_effect,
        confirmation=policy.confirmation,
    )
    async def run_script(
        script: str,
        payload: dict[str, Any],
    ) -> object:
        return await _execute_script(
            package,
            script=script,
            payload=payload,
            runtimes=policy.runtimes,
            max_output_bytes=policy.max_output_bytes,
        )

    return run_script


def skill_script_tool_name(skill: Skill) -> str:
    """Return the stable runtime Tool name for one executable Skill."""
    normalized_skill_name = skill.name.replace("-", "_").replace(".", "_")
    return f"run_{normalized_skill_name}_script"


async def _execute_script(
    package: SkillPackage,
    *,
    script: str,
    payload: dict[str, Any],
    runtimes: frozenset[SkillScriptRuntime],
    max_output_bytes: int,
) -> object:
    if script not in package.script_files:
        raise ValueError(f"Script '{script}' is not available in this Skill Package")
    script_path = package.root / script
    if script_path.is_symlink() or not script_path.is_file():
        raise ValueError(f"Script '{script}' is no longer a regular file")
    if script_path.suffix != ".py":
        raise ValueError(f"Script '{script}' has no enabled runtime in this SDK version")
    if SkillScriptRuntime.PYTHON not in runtimes:
        raise ValueError("Python Skill script execution is not enabled")

    current_package = SkillPackage.load(package.root)
    if current_package.fingerprint != package.fingerprint:
        raise ValueError("Skill Package changed after it was loaded; rebuild the Agent")

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script_path),
        cwd=str(package.root),
        env={
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        },
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    encoded_input = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        stdout, stderr = await process.communicate(encoded_input)
    except BaseException:
        await _terminate(process)
        raise

    if len(stdout) > max_output_bytes or len(stderr) > max_output_bytes:
        raise ValueError(f"Skill script output exceeded {max_output_bytes} bytes")
    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = stderr_text or f"exit code {process.returncode}"
        raise RuntimeError(f"Skill script failed: {detail}")
    output_text = stdout.decode("utf-8", errors="strict").strip()
    if not output_text:
        return None
    try:
        return json.loads(output_text)
    except json.JSONDecodeError:
        return output_text


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
    except TimeoutError:
        process.kill()
        await process.wait()
