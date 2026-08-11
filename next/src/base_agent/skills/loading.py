"""Model-visible Tool for progressively loading selected Skill content."""

from __future__ import annotations

import json

from base_agent.skills.catalog import SkillCatalog
from base_agent.tools import FunctionTool, tool

SKILL_VIEW_TOOL_NAME = "skill_view"
_MAX_SKILL_VIEW_PAYLOAD_BYTES = 15_000


def build_skill_view_tool(catalog: SkillCatalog) -> FunctionTool:
    """Build the stable runtime Tool used to load selected Skill content."""

    @tool(
        name=SKILL_VIEW_TOOL_NAME,
        description=(
            "Load the complete instructions for one available Skill, or read "
            "one of its exposed references, templates, scripts, or assets."
        ),
    )
    def view_skill(
        name: str,
        file_path: str | None = None,
    ) -> dict[str, object]:
        payload = catalog.view(name, file_path).as_dict()
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) > _MAX_SKILL_VIEW_PAYLOAD_BYTES:
            raise ValueError(
                "Skill view exceeds the model observation limit; split the "
                "playbook or supporting file into smaller linked files"
            )
        return payload

    return view_skill
