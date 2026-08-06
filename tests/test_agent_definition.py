import pytest
from pydantic import ValidationError

from base_agent import (
    Agent,
    AgentDefinition,
    AgentProfile,
    AgentResultStatus,
    EventType,
    ModelResponse,
    tool,
)
from base_agent.testing import AgentTestHarness, FakeModel


def test_agent_definition_is_versioned_immutable_and_projects_to_profile() -> None:
    definition = AgentDefinition(
        id="weather-agent",
        version="1.2.0",
        instructions="Use weather data.",
        model="frontier",
        tools=("weather",),
        skills=("weather-analysis",),
        permissions=frozenset({"weather:read"}),
        max_steps=6,
    )

    profile = definition.to_profile()

    assert profile == AgentProfile(
        id="weather-agent",
        instructions="Use weather data.",
        model="frontier",
        tools=("weather",),
        skills=("weather-analysis",),
        permissions=frozenset({"weather:read"}),
        max_steps=6,
    )
    assert definition.fingerprint == definition.fingerprint
    assert len(definition.fingerprint) == 64
    with pytest.raises(ValidationError, match="frozen"):
        definition.instructions = "Changed."  # type: ignore[misc]


def test_agent_definition_fingerprint_is_canonical_and_content_sensitive() -> None:
    first = AgentDefinition(
        id="audited-agent",
        version="1.0.0",
        instructions="Work.",
        permissions=frozenset({"data:read", "report:write"}),
    )
    reordered = AgentDefinition(
        id="audited-agent",
        version="1.0.0",
        instructions="Work.",
        permissions=frozenset({"report:write", "data:read"}),
    )
    changed = first.model_copy(update={"version": "1.0.1"})

    assert first.fingerprint == reordered.fingerprint
    assert first.fingerprint != changed.fingerprint


@pytest.mark.parametrize("version", ["", "contains spaces", "/invalid"])
def test_agent_definition_rejects_invalid_versions(version: str) -> None:
    with pytest.raises(ValidationError):
        AgentDefinition(
            id="versioned-agent",
            version=version,
            instructions="Work.",
        )


@pytest.mark.asyncio
async def test_agent_runs_from_definition_through_normal_runtime() -> None:
    @tool
    async def lookup(subject: str) -> str:
        return f"found:{subject}"

    definition = AgentDefinition(
        id="defined-agent",
        version="1.0.0",
        instructions="Use the lookup Tool.",
        tools=("lookup",),
    )
    harness = AgentTestHarness(
        Agent(
            definition=definition,
            model=FakeModel([ModelResponse(content="done")]),
            tools=(lookup,),
        )
    )

    episode = await harness.run("Find the record.")

    assert harness.agent.definition is definition
    assert harness.agent.profile == definition.to_profile()
    assert episode.result.status is AgentResultStatus.COMPLETED
    assert episode.event_types[-1] is EventType.RUN_COMPLETED


def test_agent_requires_exactly_one_definition_source() -> None:
    model = FakeModel([ModelResponse(content="unused")])
    profile = AgentProfile(id="legacy-agent", instructions="Work.")
    definition = AgentDefinition(
        id="defined-agent",
        version="1.0.0",
        instructions="Work.",
    )

    with pytest.raises(ValueError, match="exactly one"):
        Agent(model=model)
    with pytest.raises(ValueError, match="exactly one"):
        Agent(profile=profile, definition=definition, model=model)


@pytest.mark.asyncio
async def test_legacy_agent_profile_remains_supported() -> None:
    agent = Agent(
        profile=AgentProfile(id="legacy-agent", instructions="Work."),
        model=FakeModel([ModelResponse(content="done")]),
    )

    result = await agent.run("Continue.")

    assert agent.definition is None
    assert result.status is AgentResultStatus.COMPLETED
