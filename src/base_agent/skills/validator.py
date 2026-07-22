"""Shared validation used by Agent execution and developer test harnesses."""

from base_agent.profiles import AgentProfile
from base_agent.skills.errors import (
    SkillNotEnabledError,
    SkillRequirementsError,
)
from base_agent.skills.models import Skill
from base_agent.skills.registry import SkillRegistry
from base_agent.tools import ToolRegistry


def select_and_validate_skills(
    names: tuple[str, ...],
    *,
    profile: AgentProfile,
    skill_registry: SkillRegistry,
    tool_registry: ToolRegistry,
) -> tuple[Skill, ...]:
    if len(set(names)) != len(names):
        raise SkillRequirementsError("selected skills must not contain duplicates")
    disabled = set(names) - set(profile.skills)
    if disabled:
        raise SkillNotEnabledError(
            f"skills not enabled by AgentProfile: {', '.join(sorted(disabled))}"
        )

    selected = skill_registry.select(names)
    profile_tools = set(profile.tools)
    for selected_skill in selected:
        manifest = selected_skill.manifest
        unavailable_tools = set(manifest.allowed_tools) - profile_tools
        if unavailable_tools:
            raise SkillRequirementsError(
                f"Skill '{manifest.name}' tools are not enabled by AgentProfile: "
                f"{', '.join(sorted(unavailable_tools))}"
            )
        tool_registry.require(manifest.required_tools)
        missing_permissions = manifest.required_permissions - profile.permissions
        if missing_permissions:
            raise SkillRequirementsError(
                f"Skill '{manifest.name}' missing permissions: "
                f"{', '.join(sorted(missing_permissions))}"
            )
    return selected
