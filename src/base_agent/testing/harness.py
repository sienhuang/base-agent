"""Focused harnesses for testing Tools and Skills without a full Agent server."""

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from base_agent.models import ToolCall, ToolResult
from base_agent.profiles import AgentProfile
from base_agent.skills import SkillRegistry, select_and_validate_skills
from base_agent.skills.errors import (
    InvalidSkillError,
    SkillNotEnabledError,
    SkillNotFoundError,
    SkillRequirementsError,
)
from base_agent.tools import Tool, ToolExecutor, ToolRegistry
from base_agent.tools.registry import ToolNotFoundError


class SkillValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str
    valid: bool
    version: str | None = None
    issues: tuple[str, ...] = ()


class ToolHarness:
    """Execute a Tool through the same validation, permission, and timeout path as Runtime."""

    def __init__(self, tools: Iterable[Tool]) -> None:
        self.registry = ToolRegistry(tools)
        self.executor = ToolExecutor(self.registry)

    async def run(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        permissions: frozenset[str] = frozenset(),
    ) -> ToolResult:
        return await self.executor.execute(
            ToolCall(
                id=f"harness-{uuid4()}",
                name=name,
                arguments=dict(arguments or {}),
            ),
            granted_permissions=permissions,
            allowed_tools=frozenset(self.registry.names),
        )


class SkillHarness:
    """Load and validate a Skill against the exact AgentProfile and Tool contracts."""

    def __init__(self, skill_registry: SkillRegistry, tools: Iterable[Tool] = ()) -> None:
        self.skill_registry = skill_registry
        self.tool_registry = ToolRegistry(tools)

    def validate(self, name: str, *, profile: AgentProfile) -> SkillValidationReport:
        try:
            selected = select_and_validate_skills(
                (name,),
                profile=profile,
                skill_registry=self.skill_registry,
                tool_registry=self.tool_registry,
            )
        except (
            InvalidSkillError,
            SkillNotEnabledError,
            SkillNotFoundError,
            SkillRequirementsError,
            ToolNotFoundError,
        ) as exc:
            return SkillValidationReport(
                skill_name=name,
                valid=False,
                issues=(str(exc),),
            )

        return SkillValidationReport(
            skill_name=name,
            valid=True,
            version=selected[0].manifest.version,
        )
