from pathlib import Path

import pytest

from base_agent import Agent, AgentDefinition, RunStatus, Skill, SkillCatalog, tool
from base_agent.models import ModelResponse, ToolCall
from base_agent.skills import SkillContributions
from base_agent.testing import FakeModel


def _write_skill(
    root: Path,
    *,
    name: str,
    description: str = "Follow a discovered workflow.",
    body: str = "# Workflow\n\nFollow the documented steps.",
) -> Path:
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        (f"---\nname: {name}\ndescription: {description}\nversion: 1.2.0\n---\n\n{body}\n"),
        encoding="utf-8",
    )
    return root


def test_catalog_discovers_instruction_only_skills_without_importing_code(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill(
        tmp_path / "research" / "evidence-check",
        name="evidence-check",
        description="Check evidence before reaching conclusions.",
    )
    (skill_root / "definition.py").write_text(
        "raise RuntimeError('discovery must not import this file')\n",
        encoding="utf-8",
    )
    (skill_root / "tools.py").write_text(
        "raise RuntimeError('safe discovery must not import this file')\n",
        encoding="utf-8",
    )
    (skill_root / "references").mkdir()
    (skill_root / "references" / "rules.md").write_text(
        "Prefer primary evidence.\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover(tmp_path)

    assert [item.name for item in catalog.descriptors] == ["evidence-check"]
    descriptor = catalog.descriptors[0]
    assert descriptor.description == "Check evidence before reaching conclusions."
    assert descriptor.version == "1.2.0"
    assert descriptor.source == skill_root.resolve()
    skill = catalog.resolve("evidence-check")
    assert skill.playbook == "# Workflow\n\nFollow the documented steps."
    assert skill.contributions.tools == ()
    assert (
        catalog.view(
            "evidence-check",
            "references/rules.md",
        ).content
        == "Prefer primary evidence."
    )


async def test_trusted_catalog_loads_multiple_local_tools_and_their_script(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill(
        tmp_path / "calculation",
        name="calculation",
        description="Calculate values with packaged Python helpers.",
    )
    scripts = skill_root / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "operations.py").write_text(
        (
            "def add(left: int, right: int) -> int:\n"
            "    return left + right\n\n"
            "def double(value: int) -> int:\n"
            "    return value * 2\n"
        ),
        encoding="utf-8",
    )
    (skill_root / "tools.py").write_text(
        (
            "from base_agent import tool\n"
            "from .scripts.operations import add, double\n\n"
            "@tool\n"
            "async def add_values(left: int, right: int) -> int:\n"
            "    return add(left, right)\n\n"
            "@tool\n"
            "async def double_value(value: int) -> int:\n"
            "    return double(value)\n"
        ),
        encoding="utf-8",
    )
    (skill_root / "definition.py").write_text(
        "raise RuntimeError('trusted discovery loads only tools.py')\n",
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover_trusted(tmp_path)
    skill = catalog.resolve("calculation")
    assert [item.spec.name for item in skill.contributions.tools] == [
        "add_values",
        "double_value",
    ]

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="add-1",
                        name="add_values",
                        arguments={"left": 2, "right": 3},
                    ),
                    ToolCall(
                        id="double-1",
                        name="double_value",
                        arguments={"value": 4},
                    ),
                )
            ),
            ModelResponse(content="5 and 8"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="trusted-skill-agent",
            version="1.0.0",
            instructions="Use the selected Skill.",
            skills=("calculation",),
            max_model_turns=2,
        ),
        model=model,
        skills=catalog,
    )

    result = await agent.run("calculate")

    assert result.status is RunStatus.SUCCEEDED
    assert [item.data for item in result.tool_results] == [5, 8]
    assert [item.name for item in model.requests[0].tools] == [
        "skill_view",
        "add_values",
        "double_value",
    ]


def test_trusted_catalog_allows_explicit_tools_export(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path / "explicit", name="explicit")
    (skill_root / "tools.py").write_text(
        (
            "from base_agent import tool\n\n"
            "@tool\n"
            "async def public_action(value: int) -> int:\n"
            "    return value\n\n"
            "@tool\n"
            "async def private_helper(value: int) -> int:\n"
            "    return value\n\n"
            "TOOLS = (public_action,)\n"
        ),
        encoding="utf-8",
    )

    catalog = SkillCatalog.discover_trusted(tmp_path)

    assert [item.spec.name for item in catalog.resolve("explicit").contributions.tools] == [
        "public_action"
    ]


def test_trusted_catalog_reports_tools_module_import_failure(tmp_path: Path) -> None:
    skill_root = _write_skill(tmp_path / "broken", name="broken")
    (skill_root / "tools.py").write_text(
        "raise RuntimeError('broken trusted module')\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="failed to load trusted Skill tools module"):
        SkillCatalog.discover_trusted(tmp_path)


def test_catalog_discovery_uses_explicit_definition_for_code_capabilities(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill(
        tmp_path / "coded",
        name="coded",
        description="Use one application-provided Tool.",
    )

    @tool
    async def coded_action(value: int) -> int:
        return value

    definition = Skill.from_package(
        skill_root,
        contributions=SkillContributions(tools=(coded_action,)),
    )

    catalog = SkillCatalog.discover(
        tmp_path,
        definitions=(definition,),
    )

    assert catalog.resolve("coded") is definition
    assert catalog.resolve("coded").contributions.tools == (coded_action,)


def test_catalog_discovery_fails_loudly_for_ambiguous_packages(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path / "first", name="duplicate")
    _write_skill(tmp_path / "second", name="duplicate")

    with pytest.raises(ValueError, match="unique names"):
        SkillCatalog.discover(tmp_path)


def test_catalog_discovery_rejects_nested_skill_packages(
    tmp_path: Path,
) -> None:
    outer = _write_skill(tmp_path / "outer", name="outer")
    _write_skill(outer / "inner", name="inner")

    with pytest.raises(ValueError, match="nested inside Skill Package"):
        SkillCatalog.discover(tmp_path)


def test_catalog_discovery_requires_frontmatter_for_zero_code_skills(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "legacy"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "Follow the legacy instructions.\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires frontmatter fields"):
        SkillCatalog.discover(tmp_path)


def test_explicit_definition_must_match_frontmatter_identity(
    tmp_path: Path,
) -> None:
    skill_root = _write_skill(tmp_path / "identity", name="manifest-name")

    with pytest.raises(ValueError, match="name conflicts"):
        Skill.from_package(skill_root, name="different-name")
