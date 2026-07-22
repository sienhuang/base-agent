import json
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelResponse,
    Skill,
    SkillLoader,
    SkillRegistry,
    ToolCall,
    tool,
)
from base_agent.skills import (
    DuplicateSkillError,
    InvalidSkillError,
    SkillNotEnabledError,
    SkillRequirementsError,
)
from base_agent.testing import FakeModel


def test_loader_parses_versioned_manifest_and_instructions(tmp_path: Path) -> None:
    skill_file = _write_skill(
        tmp_path,
        "weather-analysis",
        allowed_tools=("weather",),
        required_tools=("weather",),
        required_permissions=("weather:read",),
        instructions="Always verify the city before calling a tool.",
    )

    skill = SkillLoader().load(skill_file)

    assert skill.manifest.name == "weather-analysis"
    assert skill.manifest.version == "1.0.0"
    assert skill.manifest.allowed_tools == ("weather",)
    assert skill.manifest.required_tools == ("weather",)
    assert skill.manifest.required_permissions == frozenset({"weather:read"})
    assert skill.instructions == "Always verify the city before calling a tool."


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("# no front matter", "must start with YAML front matter"),
        ("---\nname: demo\n", "no closing front matter delimiter"),
        (
            "---\nname: demo\nversion: 1.0.0\ndescription: Demo\n"
            "required-tools: [missing]\nallowed-tools: []\n---\nInstructions",
            "required-tools must also appear",
        ),
        (
            "---\nname: demo\nversion: 1.0.0\ndescription: Demo\n---\n",
            "must not be empty",
        ),
    ],
)
def test_loader_rejects_invalid_skill_packages(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")

    with pytest.raises(InvalidSkillError, match=message):
        SkillLoader().load(skill_file)


def test_registry_discovers_deterministically_and_rejects_duplicate_names(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path / "first", "demo", version="1.0.0")
    _write_skill(tmp_path / "second", "demo", version="2.0.0")

    with pytest.raises(DuplicateSkillError, match="already registered"):
        SkillRegistry.from_directory(tmp_path)


@pytest.mark.asyncio
async def test_skill_is_loaded_only_after_explicit_selection(tmp_path: Path) -> None:
    class CountingLoader(SkillLoader):
        def __init__(self) -> None:
            self.full_loads = 0

        def load(self, path: Path) -> Skill:
            self.full_loads += 1
            return super().load(path)

    _write_skill(tmp_path, "analysis", instructions="Use the verified analysis procedure.")
    loader = CountingLoader()
    registry = SkillRegistry.from_directory(tmp_path, loader=loader)
    model = FakeModel([ModelResponse(content="done")])
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Base instructions.",
            skills=("analysis",),
        ),
        model=model,
        skill_registry=registry,
    )

    assert loader.full_loads == 0
    result = await agent.run("work", skills=("analysis",))

    assert result.status is AgentResultStatus.COMPLETED
    assert loader.full_loads == 1
    system_prompt = model.requests[0].messages[0].content or ""
    assert "Base instructions." in system_prompt
    assert "## Skill: analysis (1.0.0)" in system_prompt
    assert "Use the verified analysis procedure." in system_prompt


@pytest.mark.asyncio
async def test_selected_skill_version_is_recorded_in_run_and_events(tmp_path: Path) -> None:
    _write_skill(tmp_path, "analysis", version="1.2.3")
    registry = SkillRegistry.from_directory(tmp_path)
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Base instructions.",
            skills=("analysis",),
        ),
        model=FakeModel([ModelResponse(content="done")]),
        skill_registry=registry,
    )

    result = await agent.run("work", skills=("analysis",))
    run_id = _uuid(result.metadata["run_id"])
    run = await agent.get_run(run_id)
    events = await agent.events(run_id)

    assert [(item.name, item.version) for item in run.skills] == [("analysis", "1.2.3")]
    skill_events = [
        event
        for event in events
        if event.type in {EventType.SKILL_SELECTED, EventType.SKILL_LOADED}
    ]
    assert [event.type for event in skill_events] == [
        EventType.SKILL_SELECTED,
        EventType.SKILL_LOADED,
    ]
    assert all(event.data == {"name": "analysis", "version": "1.2.3"} for event in skill_events)


@pytest.mark.asyncio
async def test_skill_selection_fails_before_model_for_disabled_capabilities(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, "analysis", allowed_tools=("search",))
    registry = SkillRegistry.from_directory(tmp_path)
    model = FakeModel([ModelResponse(content="must not be consumed")])
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Base instructions.",
            skills=("analysis",),
        ),
        model=model,
        skill_registry=registry,
    )

    with pytest.raises(SkillRequirementsError, match="not enabled by AgentProfile"):
        await agent.run("work", skills=("analysis",))
    with pytest.raises(SkillNotEnabledError, match="not enabled"):
        await agent.run("work", skills=("another-skill",))

    assert model.requests == ()


@pytest.mark.asyncio
async def test_skill_requires_declared_permissions_before_model_execution(tmp_path: Path) -> None:
    @tool
    async def search(query: str) -> str:
        return query

    _write_skill(
        tmp_path,
        "analysis",
        allowed_tools=("search",),
        required_tools=("search",),
        required_permissions=("search:read",),
    )
    registry = SkillRegistry.from_directory(tmp_path)
    model = FakeModel([ModelResponse(content="must not be consumed")])
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Base instructions.",
            tools=("search",),
            skills=("analysis",),
        ),
        model=model,
        tools=[search],
        skill_registry=registry,
    )

    with pytest.raises(SkillRequirementsError, match="missing permissions: search:read"):
        await agent.run("work", skills=("analysis",))

    assert model.requests == ()


@pytest.mark.asyncio
async def test_skill_allowlist_is_enforced_even_for_hallucinated_tool_call(
    tmp_path: Path,
) -> None:
    secret_executed = False

    @tool
    async def allowed(value: str) -> str:
        return value

    @tool
    async def secret() -> str:
        nonlocal secret_executed
        secret_executed = True
        return "secret"

    _write_skill(tmp_path, "safe-analysis", allowed_tools=("allowed",))
    registry = SkillRegistry.from_directory(tmp_path)
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(ToolCall(id="call-1", name="secret", arguments={}),)
            ),
            ModelResponse(content="recovered"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Base instructions.",
            tools=("allowed", "secret"),
            skills=("safe-analysis",),
        ),
        model=model,
        tools=[allowed, secret],
        skill_registry=registry,
    )

    result = await agent.run("work", skills=("safe-analysis",))

    assert result.status is AgentResultStatus.COMPLETED
    assert secret_executed is False
    assert [definition.name for definition in model.requests[0].tools] == ["allowed"]
    tool_result = json.loads(model.requests[1].messages[-1].content or "{}")
    assert tool_result["error_code"] == "tool_not_allowed"


def _write_skill(
    root: Path,
    name: str,
    *,
    version: str = "1.0.0",
    allowed_tools: tuple[str, ...] = (),
    required_tools: tuple[str, ...] = (),
    required_permissions: tuple[str, ...] = (),
    instructions: str = "Follow the documented procedure.",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    skill_file = root / "SKILL.md"
    manifest = {
        "name": name,
        "version": version,
        "description": f"{name} description",
        "allowed-tools": list(allowed_tools),
        "required-tools": list(required_tools),
        "required-permissions": list(required_permissions),
    }
    skill_file.write_text(
        f"---\n{yaml.safe_dump(manifest, sort_keys=False)}---\n{instructions}\n",
        encoding="utf-8",
    )
    return skill_file


def _uuid(value: object) -> UUID:
    return UUID(str(value))
