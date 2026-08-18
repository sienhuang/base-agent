import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import BaseModel

from base_agent import (
    Agent,
    AgentDefinition,
    RunStatus,
    Skill,
    SkillCatalog,
    ToolChoice,
    Toolkit,
    ToolReadiness,
    tool,
)
from base_agent.models import (
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ToolCall,
    ToolResultStatus,
)
from base_agent.providers import ProviderCapabilities, ProviderInfo
from base_agent.skills import (
    SkillContributions,
    SkillDependencies,
    SkillPermissions,
    SkillScriptExecution,
    SkillScriptRuntime,
)
from base_agent.testing import FakeModel
from base_agent.tools.core import ToolContext


async def test_tool_context_is_hidden_from_schema_and_injected_at_runtime() -> None:
    seen: list[ToolContext] = []

    @tool
    async def context_tool(value: int, context: ToolContext) -> int:
        seen.append(context)
        return value

    assert set(context_tool.spec.input_schema["properties"]) == {"value"}
    context = ToolContext(run_id="run-1", agent_id="agent-1", tool_call_id="call-1")
    assert await context_tool.invoke({"value": 7}, context) == 7
    assert seen == [context]


def test_toolkit_rejects_blank_name_and_duplicate_tools() -> None:
    @tool
    async def ping() -> str:
        return "pong"

    with pytest.raises(ValueError, match="must not be blank"):
        Toolkit.of(" ", ping)
    with pytest.raises(ValueError, match="duplicate tools"):
        Toolkit.of("duplicate", ping, ping)


def test_agent_rejects_duplicate_tools_across_toolkit_boundary() -> None:
    @tool
    async def ping() -> str:
        return "pong"

    with pytest.raises(
        ValueError,
        match="registered tools must have unique names",
    ):
        Agent(
            AgentDefinition(
                id="duplicate-tools",
                version="1.0.0",
                instructions="Ping.",
                tools=("ping",),
            ),
            model=FakeModel.text("unused"),
            tools=(Toolkit.of("network", ping), ping),
        )


async def test_skill_package_contributes_owned_tool_and_resource_inventory(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "calculation"
    (package_root / "scripts").mkdir(parents=True)
    (package_root / "references").mkdir()
    (package_root / "assets").mkdir()
    (package_root / "templates").mkdir()
    (package_root / "SKILL.md").write_text(
        "Use calculate_value for deterministic calculations.",
        encoding="utf-8",
    )
    (package_root / "scripts" / "calculate.py").write_text(
        "def calculate(value): return value * 2\n",
        encoding="utf-8",
    )
    (package_root / "scripts" / "render.sh").write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$1\"\n",
        encoding="utf-8",
    )
    (package_root / "references" / "rules.md").write_text(
        "Preserve integer precision.\n",
        encoding="utf-8",
    )
    (package_root / "assets" / "template.txt").write_text(
        "result={value}\n",
        encoding="utf-8",
    )
    (package_root / "templates" / "report.md").write_text(
        "# Result\n",
        encoding="utf-8",
    )

    @tool
    async def calculate_value(value: int) -> int:
        """Calculate one deterministic value."""
        return value * 2

    calculation = Skill.from_package(
        package_root,
        name="calculation",
        contributions=SkillContributions(tools=(calculate_value,)),
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="calculate-1",
                        name="calculate_value",
                        arguments={"value": 2},
                    ),
                )
            ),
            ModelResponse(content="4"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="calculation-agent",
            version="1.0.0",
            instructions="Calculate accurately.",
            skills=("calculation",),
            max_model_turns=2,
        ),
        model=model,
        skills=(calculation,),
    )

    result = await agent.run("double 2")

    assert result.status is RunStatus.SUCCEEDED
    assert result.tool_results[0].data == 4
    assert [tool.name for tool in model.requests[0].tools] == ["skill_view", "calculate_value"]
    assert result.snapshot.skill_names == ("calculation",)
    skill_snapshot = result.snapshot.skills[0]
    assert len(skill_snapshot.package_fingerprint or "") == 64
    assert skill_snapshot.scripts == (
        "scripts/calculate.py",
        "scripts/render.sh",
    )
    assert skill_snapshot.references == ("references/rules.md",)
    assert skill_snapshot.templates == ("templates/report.md",)
    assert skill_snapshot.assets == ("assets/template.txt",)
    skill_events = [
        event for event in result.events if event.kind in {"skill.selected", "skill.resource.used"}
    ]
    assert [event.kind for event in skill_events] == [
        "skill.selected",
        "skill.resource.used",
    ]
    assert skill_events[0].payload["skill_name"] == "calculation"
    assert skill_events[0].turn is None
    assert skill_events[1].turn == 1
    assert skill_events[1].payload == {
        "skill_name": "calculation",
        "skill_version": "1.0.0",
        "package_fingerprint": skill_snapshot.package_fingerprint,
        "resource_type": "tool",
        "resource": "calculate_value",
        "tool_name": "calculate_value",
        "tool_call_id": "calculate-1",
    }

    original_fingerprint = skill_snapshot.package_fingerprint
    (package_root / "references" / "rules.md").write_text(
        "Preserve integer precision and units.\n",
        encoding="utf-8",
    )
    reloaded = Skill.from_package(
        package_root,
        name="calculation",
        contributions=calculation.contributions,
    )
    assert reloaded.package is not None
    assert reloaded.package.fingerprint != original_fingerprint


async def test_skill_catalog_exposes_descriptors_and_skill_view_loads_playbook(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "review"
    (package_root / "references").mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        "Inspect evidence before reaching a conclusion.",
        encoding="utf-8",
    )
    (package_root / "references" / "checklist.md").write_text(
        "Verify the behavior with a focused test.\n",
        encoding="utf-8",
    )
    skill = Skill.from_package(
        package_root,
        name="evidence-review",
        description="Review one change using direct evidence.",
    )
    catalog = SkillCatalog.of(skill)
    descriptor = catalog.descriptors[0]
    assert descriptor.name == "evidence-review"
    assert descriptor.description == "Review one change using direct evidence."
    assert descriptor.source == package_root.resolve()
    assert not hasattr(descriptor, "playbook")

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="load-review",
                        name="skill_view",
                        arguments={"name": "evidence-review"},
                    ),
                )
            ),
            ModelResponse(content="reviewed"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="review-agent",
            version="1.0.0",
            instructions="Review carefully.",
            skills=("evidence-review",),
            max_model_turns=2,
        ),
        model=model,
        skills=catalog,
    )

    result = await agent.run("review this")

    assert result.status is RunStatus.SUCCEEDED
    assert skill.playbook not in result.snapshot.system_prompt
    assert "`evidence-review` (version 1.0.0)" in (result.snapshot.system_prompt)
    assert [tool.name for tool in model.requests[0].tools] == ["skill_view"]
    assert result.tool_results[0].data == {
        "name": "evidence-review",
        "description": "Review one change using direct evidence.",
        "version": "1.0.0",
        "package_fingerprint": descriptor.package_fingerprint,
        "file": "SKILL.md",
        "content": skill.playbook,
        "linked_files": {
            "references": ["references/checklist.md"],
        },
    }
    skill_events = [event for event in result.events if event.kind.startswith("skill.")]
    assert [event.kind for event in skill_events] == [
        "skill.selected",
        "skill.loaded",
    ]
    assert skill_events[1].turn == 1
    assert skill_events[1].payload == {
        "skill_name": "evidence-review",
        "skill_version": "1.0.0",
        "package_fingerprint": descriptor.package_fingerprint,
        "file": "SKILL.md",
        "tool_call_id": "load-review",
    }


def test_skill_view_reads_only_exposed_supporting_files(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "documented"
    (package_root / "references").mkdir(parents=True)
    (package_root / "SKILL.md").write_text("Read the checklist.", encoding="utf-8")
    (package_root / "references" / "checklist.md").write_text(
        "Check the final result.\n",
        encoding="utf-8",
    )
    skill = Skill.from_package(package_root, name="documented")
    catalog = SkillCatalog.of(skill)

    view = catalog.view("documented", "references/checklist.md")

    assert view.content == "Check the final result."
    with pytest.raises(ValueError, match="relative path"):
        catalog.view("documented", "../secret.txt")
    with pytest.raises(ValueError, match="not an exposed supporting file"):
        catalog.view("documented", "references/missing.md")


async def test_skill_view_emits_file_loaded_for_a_supporting_file(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "supporting-event"
    (package_root / "references").mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        "Load the reference when needed.",
        encoding="utf-8",
    )
    (package_root / "references" / "rules.md").write_text(
        "Use verified inputs.\n",
        encoding="utf-8",
    )
    skill = Skill.from_package(package_root, name="supporting-event")
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="load-rules",
                        name="skill_view",
                        arguments={
                            "name": "supporting-event",
                            "file_path": "references/rules.md",
                        },
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="supporting-event-agent",
            version="1.0.0",
            instructions="Use selected references.",
            skills=("supporting-event",),
            max_model_turns=2,
        ),
        model=model,
        skills=SkillCatalog.of(skill),
    )

    result = await agent.run("load rules")

    assert result.tool_results[0].data["content"] == "Use verified inputs."
    file_events = [event for event in result.events if event.kind == "skill.file.loaded"]
    assert file_events[0].payload["file"] == "references/rules.md"
    assert file_events[0].payload["tool_call_id"] == "load-rules"


async def test_oversized_skill_view_fails_without_emitting_loaded() -> None:
    skill = Skill(
        name="oversized",
        description="An intentionally oversized playbook.",
        playbook="x" * 16_000,
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="load-oversized",
                        name="skill_view",
                        arguments={"name": "oversized"},
                    ),
                )
            ),
            ModelResponse(content="could not load"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="oversized-agent",
            version="1.0.0",
            instructions="Load selected Skills.",
            skills=("oversized",),
            max_model_turns=2,
        ),
        model=model,
        skills=SkillCatalog.of(skill),
    )

    result = await agent.run("load it")

    assert result.tool_results[0].status is ToolResultStatus.ERROR
    assert "observation limit" in (result.tool_results[0].error or "")
    assert "skill.loaded" not in [event.kind for event in result.events]


def test_selected_skills_reject_tool_choice_none() -> None:
    skill = Skill(name="instructions", playbook="Follow the instructions.")

    with pytest.raises(ValueError, match="require Tool use"):
        Agent(
            AgentDefinition(
                id="no-skill-tools",
                version="1.0.0",
                instructions="Answer.",
                skills=("instructions",),
                tool_choice=ToolChoice.NONE,
            ),
            model=FakeModel.text("unused"),
            skills=SkillCatalog.of(skill),
        )


def test_skill_package_requires_skill_markdown(tmp_path: Path) -> None:
    package_root = tmp_path / "missing-instructions"
    package_root.mkdir()

    with pytest.raises(
        ValueError,
        match="missing required SKILL.md",
    ):
        Skill.from_package(package_root, name="missing-instructions")


def test_agent_rejects_duplicate_direct_and_skill_owned_tool() -> None:
    @tool
    async def ping() -> str:
        return "pong"

    skill = Skill(
        name="ping-skill",
        playbook="Use ping.",
        contributions=SkillContributions(tools=(ping,)),
    )

    with pytest.raises(
        ValueError,
        match="registered tools must have unique names",
    ):
        Agent(
            AgentDefinition(
                id="duplicate-skill-tool",
                version="1.0.0",
                instructions="Ping.",
                skills=("ping-skill",),
            ),
            model=FakeModel.text("unused"),
            tools=(ping,),
            skills=(skill,),
        )


def test_skill_required_tool_is_an_explicit_external_dependency() -> None:
    skill = Skill(
        name="browser-usage",
        playbook="Use the shared browser Tool.",
        dependencies=SkillDependencies(required_tools=("browser",)),
    )

    with pytest.raises(
        ValueError,
        match="references unregistered Tools: browser",
    ):
        Agent(
            AgentDefinition(
                id="browser-agent",
                version="1.0.0",
                instructions="Browse safely.",
                skills=("browser-usage",),
            ),
            model=FakeModel.text("unused"),
            skills=(skill,),
        )


async def test_optional_skill_dependency_is_used_when_available() -> None:
    @tool
    async def shared_lookup(query: str) -> str:
        """Look up one shared value."""
        return query

    skill = Skill(
        name="research-playbook",
        playbook="Use shared_lookup when it is available.",
        dependencies=SkillDependencies(optional_tools=("shared_lookup",)),
        permissions=SkillPermissions(optional=frozenset({"research:extended"})),
    )
    model = FakeModel.text("done")
    agent = Agent(
        AgentDefinition(
            id="research-agent",
            version="1.0.0",
            instructions="Research carefully.",
            skills=("research-playbook",),
        ),
        model=model,
        tools=(shared_lookup,),
        skills=(skill,),
    )

    result = await agent.run("research")

    assert result.status is RunStatus.SUCCEEDED
    assert [tool.name for tool in model.requests[0].tools] == ["skill_view", "shared_lookup"]
    assert result.snapshot.skills[0].optional_tools == ("shared_lookup",)
    assert result.snapshot.skills[0].optional_permissions == ("research:extended",)


async def test_missing_optional_skill_dependency_allows_degraded_run() -> None:
    skill = Skill(
        name="degraded-research",
        playbook="Use shared_lookup if available; otherwise answer directly.",
        dependencies=SkillDependencies(optional_tools=("shared_lookup",)),
    )
    model = FakeModel.text("direct answer")
    agent = Agent(
        AgentDefinition(
            id="degraded-research-agent",
            version="1.0.0",
            instructions="Research carefully.",
            skills=("degraded-research",),
        ),
        model=model,
        skills=(skill,),
    )

    result = await agent.run("research")

    assert result.status is RunStatus.SUCCEEDED
    assert [tool.name for tool in model.requests[0].tools] == ["skill_view"]


async def test_skill_directly_executes_auto_discovered_python_script(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "direct-python"
    (package_root / "scripts").mkdir(parents=True)
    (package_root / "SKILL.md").write_text(
        "Run scripts/double.py when asked to double a value.",
        encoding="utf-8",
    )
    (package_root / "scripts" / "double.py").write_text(
        (
            "import json, sys\n"
            "payload = json.load(sys.stdin)\n"
            "json.dump({'result': payload['value'] * 2}, sys.stdout)\n"
        ),
        encoding="utf-8",
    )
    skill = Skill.from_package(
        package_root,
        name="direct-python",
        permissions=SkillPermissions(required=frozenset({"skills:scripts:execute"})),
        script_execution=SkillScriptExecution(
            runtimes=frozenset({SkillScriptRuntime.PYTHON}),
        ),
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="script-1",
                        name="run_direct_python_script",
                        arguments={
                            "script": "scripts/double.py",
                            "payload": {"value": 4},
                        },
                    ),
                )
            ),
            ModelResponse(content="8"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="direct-python-agent",
            version="1.0.0",
            instructions="Use the selected Skill.",
            skills=("direct-python",),
            permissions=frozenset({"skills:scripts:execute"}),
            max_model_turns=2,
        ),
        model=model,
        skills=(skill,),
    )

    result = await agent.run("double 4")

    assert result.status is RunStatus.SUCCEEDED
    assert result.tool_results[0].status is ToolResultStatus.SUCCESS
    assert result.tool_results[0].data == {"result": 8}
    assert result.snapshot.skills[0].script_runtimes == ("python",)
    assert [tool.name for tool in model.requests[0].tools] == [
        "skill_view",
        "run_direct_python_script",
    ]
    skill_events = [
        event for event in result.events if event.kind in {"skill.selected", "skill.resource.used"}
    ]
    assert [event.kind for event in skill_events] == [
        "skill.selected",
        "skill.resource.used",
    ]
    assert skill_events[1].payload["skill_name"] == "direct-python"
    assert skill_events[1].payload["resource_type"] == "script"
    assert skill_events[1].payload["resource"] == "scripts/double.py"
    assert skill_events[1].payload["tool_name"] == ("run_direct_python_script")
    assert skill_events[1].payload["tool_call_id"] == "script-1"


async def test_minimal_agent_freezes_snapshot_and_emits_ordered_events() -> None:
    observed: list[str] = []
    per_run_observed = []

    def observe(event) -> None:
        observed.append(event.kind)

    model = FakeModel.text("Hello.")
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer briefly.",
        ),
        model=model,
        observers=(observe,),
    )

    result = await agent.run("Hi", on_event=per_run_observed.append)

    assert result.status is RunStatus.SUCCEEDED
    assert result.output == "Hello."
    assert result.snapshot.system_prompt == "Answer briefly."
    assert len(result.snapshot.fingerprint) == 64
    assert [event.sequence for event in result.events] == list(range(1, len(result.events) + 1))
    assert [event.turn for event in result.events] == [None, None, None, 1, 1, 1, 1, 1]
    assert all("turn" not in event.payload for event in result.events)
    assert observed == [event.kind for event in result.events]
    assert per_run_observed == list(result.events)
    assert model.requests[0].system_prompt == result.snapshot.system_prompt


async def test_agent_stream_publishes_live_text_deltas_and_result() -> None:
    class StreamingTextModel:
        def __init__(self) -> None:
            self.requests: list[ModelRequest] = []

        @property
        def info(self) -> ProviderInfo:
            return ProviderInfo(
                name="streaming-fake",
                default_model="streaming-fake",
                capabilities=ProviderCapabilities(streaming=True),
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming run must not call complete()")

        async def stream(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelStreamEvent]:
            self.requests.append(request)
            yield ModelTextDelta("Hel")
            yield ModelTextDelta("lo")
            yield ModelStreamCompleted(ModelResponse(content="Hello"))

    model = StreamingTextModel()
    observed = []
    agent = Agent(
        AgentDefinition(
            id="streaming-assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
    )

    result = await agent.stream(
        "hello",
        on_event=observed.append,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "Hello"
    created = next(event for event in observed if event.kind == "message.created")
    deltas = [event for event in observed if event.kind == "message.delta"]
    completed = next(event for event in observed if event.kind == "message.completed")

    assert created.payload["role"] == "user"
    assert created.payload["content"] == "hello"
    assert [event.payload["delta"] for event in deltas] == ["Hel", "lo"]
    assert {event.payload["message_id"] for event in deltas} == {
        completed.payload["message_id"]
    }
    assert completed.payload["role"] == "assistant"
    assert completed.payload["content"] == "Hello"
    event_kinds = [event.kind for event in observed]
    assert event_kinds.index("model.requested") < event_kinds.index("message.delta")
    assert event_kinds.index("message.delta") < event_kinds.index("model.responded")
    assert event_kinds.index("model.responded") < event_kinds.index("message.completed")
    assert event_kinds.index("message.completed") < event_kinds.index("run.succeeded")
    assert model.requests[0].messages[0].content == "hello"


async def test_agent_stream_rejects_non_streaming_provider() -> None:
    agent = Agent(
        AgentDefinition(
            id="non-streaming-assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("not streamed"),
    )

    with pytest.raises(ValueError, match="does not support streaming"):
        await agent.stream("hello", on_event=lambda event: None)


async def test_agent_stream_fails_when_completed_text_mismatches_deltas() -> None:
    class BrokenStreamingModel:
        @property
        def info(self) -> ProviderInfo:
            return ProviderInfo(
                name="broken-stream",
                default_model="broken-stream",
                capabilities=ProviderCapabilities(streaming=True),
            )

        async def complete(self, request: ModelRequest) -> ModelResponse:
            raise AssertionError("streaming run must not call complete()")

        async def stream(
            self,
            request: ModelRequest,
        ) -> AsyncIterator[ModelStreamEvent]:
            yield ModelTextDelta("partial")
            yield ModelStreamCompleted(ModelResponse(content="different final text"))

    observed = []
    agent = Agent(
        AgentDefinition(
            id="broken-stream-assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=BrokenStreamingModel(),
    )

    result = await agent.stream(
        "hello",
        on_event=observed.append,
    )

    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == "invalid_provider_response"
    assert [
        event.kind
        for event in observed
        if event.kind
        in {
            "message.delta",
            "model.failed",
            "run.failed",
        }
    ] == [
        "message.delta",
        "model.failed",
        "run.failed",
    ]
    assert {event.turn for event in observed if event.kind in {"model.failed", "run.failed"}} == {
        1
    }
    assert all("turn" not in event.payload for event in observed)


async def test_skill_enables_tool_and_harness_executes_model_tool_loop() -> None:
    @tool(permissions=frozenset({"weather:read"}))
    async def get_weather(city: str) -> str:
        """Get current weather for a city."""
        return f"{city} is sunny"

    weather = Skill(
        name="weather",
        description="Answer weather questions.",
        playbook="Use get_weather before answering.",
        dependencies=SkillDependencies(required_tools=("get_weather",)),
        permissions=SkillPermissions(required=frozenset({"weather:read"})),
    )
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="get_weather",
                        arguments={"city": "Shanghai"},
                    ),
                )
            ),
            ModelResponse(content="Shanghai is sunny."),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="weather-agent",
            version="1.0.0",
            instructions="Be accurate.",
            skills=("weather",),
            permissions=frozenset({"weather:read"}),
        ),
        model=model,
        tools=(get_weather,),
        skills=(weather,),
    )

    result = await agent.run("What is the weather?")

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "Shanghai is sunny."
    assert result.tool_results[0].data == "Shanghai is sunny"
    assert result.snapshot.skill_names == ("weather",)
    assert weather.playbook not in result.snapshot.system_prompt
    assert "`weather` (version 1.0.0)" in result.snapshot.system_prompt
    assert [spec.name for spec in model.requests[0].tools] == [
        "skill_view",
        "get_weather",
    ]
    assert model.requests[1].messages[-1].role == "tool"


async def test_harness_executes_only_the_first_tool_calls_per_model_turn() -> None:
    executed: list[int] = []

    @tool
    async def record(value: int) -> int:
        executed.append(value)
        return value

    calls = tuple(
        ToolCall(
            id=f"call-{value}",
            name="record",
            arguments={"value": value},
        )
        for value in range(1, 6)
    )
    model = FakeModel(
        [
            ModelResponse(tool_calls=calls),
            ModelResponse(content="Used the available observations."),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="bounded-tools",
            version="1.0.0",
            instructions="Use bounded Tools.",
            tools=("record",),
            max_model_turns=2,
            max_tool_calls_per_turn=3,
        ),
        model=model,
        tools=(record,),
    )

    result = await agent.run("record five values")

    assert result.status is RunStatus.SUCCEEDED
    assert executed == [1, 2, 3]
    assert [item.status for item in result.tool_results] == [
        ToolResultStatus.SUCCESS,
        ToolResultStatus.SUCCESS,
        ToolResultStatus.SUCCESS,
        ToolResultStatus.LIMIT_EXCEEDED,
        ToolResultStatus.LIMIT_EXCEEDED,
    ]
    assert [item.call.id for item in result.tool_results] == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
        "call-5",
    ]
    final_request = model.requests[1]
    assert final_request.tools == ()
    assert final_request.tool_choice is ToolChoice.NONE
    tool_messages = [message for message in final_request.messages if message.role == "tool"]
    assert [message.tool_call_id for message in tool_messages] == [
        "call-1",
        "call-2",
        "call-3",
        "call-4",
        "call-5",
    ]
    assert json.loads(tool_messages[-1].content)["status"] == ("limit_exceeded")
    assert result.snapshot.max_model_turns == 2
    assert result.snapshot.max_tool_calls_per_turn == 3
    limited = [event for event in result.events if event.kind == "tool.batch_limited"]
    assert limited[0].turn == 1
    assert limited[0].payload == {
        "requested": 5,
        "accepted": 3,
        "skipped": 2,
        "limit": 3,
    }


async def test_agent_definition_can_raise_the_run_observation_budget() -> None:
    @tool
    async def large_observation(value: int) -> str:
        return f"{value}:" + ("x" * 20_000)

    calls = tuple(
        ToolCall(
            id=f"large-{value}",
            name="large_observation",
            arguments={"value": value},
        )
        for value in range(5)
    )
    model = FakeModel(
        [
            ModelResponse(tool_calls=calls),
            ModelResponse(content="processed large metadata"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="large-observation-agent",
            version="1.0.0",
            instructions="Process bounded metadata.",
            tools=("large_observation",),
            max_model_turns=2,
            max_tool_calls_per_turn=5,
            max_tool_observation_bytes=16_384,
            max_run_observation_bytes=131_072,
        ),
        model=model,
        tools=(large_observation,),
    )

    result = await agent.run("process metadata")

    assert result.status is RunStatus.SUCCEEDED
    assert result.snapshot.max_tool_observation_bytes == 16_384
    assert result.snapshot.max_run_observation_bytes == 131_072
    tool_messages = [
        message for message in model.requests[-1].messages if message.role == "tool"
    ]
    assert len(tool_messages) == 5
    assert all(json.loads(message.content)["truncated"] for message in tool_messages)


async def test_run_observation_budget_failure_is_structured() -> None:
    @tool
    async def large_observation() -> str:
        return "x" * 20_000

    agent = Agent(
        AgentDefinition(
            id="bounded-observation-agent",
            version="1.0.0",
            instructions="Process bounded metadata.",
            tools=("large_observation",),
            max_model_turns=2,
            max_tool_calls_per_turn=3,
            max_tool_observation_bytes=16_384,
            max_run_observation_bytes=32_768,
        ),
        model=FakeModel(
            [
                ModelResponse(
                    tool_calls=tuple(
                        ToolCall(
                            id=f"large-{index}",
                            name="large_observation",
                            arguments={},
                        )
                        for index in range(3)
                    )
                ),
                ModelResponse(content="not reached"),
            ]
        ),
        tools=(large_observation,),
    )

    result = await agent.run("process metadata")

    assert result.status is RunStatus.FAILED
    assert result.failure is not None
    assert result.failure.code == "tool_observation_budget_exceeded"
    assert result.failure.details["max_run_observation_bytes"] == 32_768
    assert result.failure.details["observed_bytes"] > 32_768


def test_agent_definition_rejects_invalid_observation_budgets() -> None:
    with pytest.raises(ValueError, match="at least 1024"):
        AgentDefinition(
            id="small-observation",
            version="1.0.0",
            instructions="Answer.",
            max_tool_observation_bytes=1_023,
        )
    with pytest.raises(ValueError, match="must be at least max_tool"):
        AgentDefinition(
            id="inverted-observation",
            version="1.0.0",
            instructions="Answer.",
            max_tool_observation_bytes=4_096,
            max_run_observation_bytes=2_048,
        )


async def test_final_model_turn_never_executes_more_tools() -> None:
    executed: list[int] = []

    @tool
    async def record(value: int) -> int:
        executed.append(value)
        return value

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="record",
                        arguments={"value": 1},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-2",
                        name="record",
                        arguments={"value": 2},
                    ),
                )
            ),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="final-turn",
            version="1.0.0",
            instructions="Use the Tool.",
            tools=("record",),
            max_model_turns=2,
        ),
        model=model,
        tools=(record,),
    )

    result = await agent.run("record")

    assert result.status is RunStatus.FAILED
    assert result.error == ("final model turn requested Tools while Tool use was disabled")
    assert executed == [1]
    assert model.requests[1].tools == ()
    assert model.requests[1].tool_choice is ToolChoice.NONE


async def test_required_tool_choice_only_applies_until_one_tool_is_called() -> None:
    @tool
    async def lookup(query: str) -> str:
        return query

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="lookup",
                        arguments={"query": "answer"},
                    ),
                )
            ),
            ModelResponse(content="done"),
        ]
    )
    agent = Agent(
        AgentDefinition(
            id="required-tool",
            version="1.0.0",
            instructions="Use lookup.",
            tools=("lookup",),
            tool_choice=ToolChoice.REQUIRED,
            max_model_turns=3,
        ),
        model=model,
        tools=(lookup,),
    )

    result = await agent.run("lookup")

    assert result.status is RunStatus.SUCCEEDED
    assert model.requests[0].tool_choice is ToolChoice.REQUIRED
    assert model.requests[1].tool_choice is ToolChoice.AUTO


def test_agent_with_tools_reserves_a_final_model_turn() -> None:
    @tool
    async def lookup(query: str) -> str:
        return query

    with pytest.raises(ValueError, match="max_model_turns >= 2"):
        Agent(
            AgentDefinition(
                id="invalid-budget",
                version="1.0.0",
                instructions="Use lookup.",
                tools=("lookup",),
                max_model_turns=1,
            ),
            model=FakeModel.text("unused"),
            tools=(lookup,),
        )


async def test_readiness_fails_before_the_model_is_called() -> None:
    @tool(readiness=lambda: ToolReadiness(False, "browser is not installed"))
    async def browse(url: str) -> str:
        return url

    model = FakeModel.text("must not be called")
    agent = Agent(
        AgentDefinition(
            id="browser-agent",
            version="1.0.0",
            instructions="Browse.",
            tools=("browse",),
        ),
        model=model,
        tools=(browse,),
    )

    result = await agent.run("Open the page")

    assert result.status is RunStatus.FAILED
    assert result.error == "Tool 'browse' is not ready: browser is not installed"
    assert model.requests == ()
    assert "tool.not_ready" in [event.kind for event in result.events]


async def test_observer_failure_cannot_change_agent_behavior() -> None:
    def broken_observer(event) -> None:
        raise RuntimeError("telemetry unavailable")

    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=FakeModel.text("ok"),
        observers=(broken_observer,),
    )

    result = await agent.run("Hi")

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "ok"


class Answer(BaseModel):
    value: int


async def test_structured_output_is_verified_by_the_harness() -> None:
    model = FakeModel.text('{"value": 42}')
    agent = Agent(
        AgentDefinition(
            id="structured",
            version="1.0.0",
            instructions="Return JSON.",
            tool_choice=ToolChoice.NONE,
            response_model=Answer,
        ),
        model=model,
    )

    result = await agent.run("answer")

    assert result.status is RunStatus.SUCCEEDED
    assert result.output == Answer(value=42)
    assert result.evidence[0].status == "passed"
    assert model.requests[0].response_schema == Answer.model_json_schema()
