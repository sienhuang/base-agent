import subprocess
import sys
from pathlib import Path

import pytest

from base_agent import AgentProfile, SkillRegistry, ToolResultStatus, tool
from base_agent.testing import SkillHarness, ToolHarness

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.asyncio
async def test_tool_harness_uses_runtime_validation_and_permissions() -> None:
    @tool(permissions=frozenset({"math:use"}))
    async def double(value: int) -> int:
        return value * 2

    harness = ToolHarness([double])

    denied = await harness.run("double", {"value": 2})
    invalid = await harness.run(
        "double",
        {"value": "invalid"},
        permissions=frozenset({"math:use"}),
    )
    successful = await harness.run(
        "double",
        {"value": 2},
        permissions=frozenset({"math:use"}),
    )

    assert denied.status is ToolResultStatus.DENIED
    assert invalid.status is ToolResultStatus.INVALID_ARGUMENTS
    assert successful.status is ToolResultStatus.SUCCESS
    assert successful.data == 4


def test_skill_harness_reports_valid_and_invalid_profiles() -> None:
    skill_root = PROJECT_ROOT / "examples" / "skill_agent" / "skills"
    registry = SkillRegistry.from_directory(skill_root)

    @tool(permissions=frozenset({"weather:read"}))
    async def get_weather(city: str) -> str:
        return city

    harness = SkillHarness(registry, [get_weather])
    valid_profile = AgentProfile(
        id="weather-agent",
        instructions="Follow the Skill.",
        tools=("get_weather",),
        skills=("weather-analysis",),
        permissions=frozenset({"weather:read"}),
    )
    invalid_profile = valid_profile.model_copy(update={"permissions": frozenset()})

    valid = harness.validate("weather-analysis", profile=valid_profile)
    invalid = harness.validate("weather-analysis", profile=invalid_profile)

    assert valid.valid is True
    assert valid.version == "1.0.0"
    assert invalid.valid is False
    assert "missing permissions" in invalid.issues[0]


@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("examples/hello_agent.py", "Hello from base-agent!"),
        ("examples/tool_agent.py", "Shanghai is sunny."),
        ("examples/skill_agent/run.py", "Verified result: Shanghai is sunny."),
    ],
)
def test_offline_examples_are_executable(script: str, expected: str) -> None:
    completed = subprocess.run(
        [sys.executable, script],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.stdout.strip() == expected
    assert completed.stderr == ""
