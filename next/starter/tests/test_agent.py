import json
from dataclasses import replace

import pytest
from base_agent import RunStatus
from base_agent.governance import AllowAllConfirmations
from base_agent.models import ModelResponse, ToolCall, ToolResultStatus
from base_agent.providers import ProviderInfo
from base_agent.testing import FakeModel

from agent_app.app import (
    ENABLED_PERMISSIONS,
    ENABLED_SKILL_NAMES,
    STARTER_AGENT_DEFINITION,
    build_agent,
    run,
)
from agent_app.config import Settings
from agent_app.provider import (
    OfflineProvider,
    build_plan_react_providers,
    build_provider,
)
from agent_app.skills import (
    PYTHON_CALCULATOR_SKILL_NAME,
    SKILL_CATALOG,
    VALUE_RECORDING_SKILL_NAME,
)
from agent_app.tools import DIAGNOSTIC_TOOLKIT, ENABLED_TOOL_NAMES, check_browser_readiness


def test_application_groups_diagnostic_tools_in_a_toolkit() -> None:
    assert DIAGNOSTIC_TOOLKIT.name == "diagnostics"
    assert [item.spec.name for item in DIAGNOSTIC_TOOLKIT.tools] == [
        "fail_operation",
        "slow_operation",
    ]


def test_application_registers_lightweight_skill_descriptors() -> None:
    assert [item.name for item in SKILL_CATALOG.descriptors] == [
        "python-calculator",
        "value-recording",
    ]
    assert all(not hasattr(item, "playbook") for item in SKILL_CATALOG.descriptors)


def test_starter_definition_is_the_single_capability_policy() -> None:
    assert STARTER_AGENT_DEFINITION.tools == ENABLED_TOOL_NAMES
    assert STARTER_AGENT_DEFINITION.skills == ENABLED_SKILL_NAMES
    assert STARTER_AGENT_DEFINITION.permissions == ENABLED_PERMISSIONS


def test_application_rejects_unknown_enabled_skill() -> None:
    with pytest.raises(ValueError, match="references unregistered Skills: missing-skill"):
        build_agent(
            definition=replace(
                STARTER_AGENT_DEFINITION,
                skills=("missing-skill",),
            )
        )


async def test_default_offline_agent_runs_end_to_end() -> None:
    result = await run("hello")

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "Starter Agent received: hello"
    assert result.snapshot.agent_id == "starter-assistant"
    assert result.snapshot.model_name == "starter-offline"
    assert result.snapshot.skill_names == ENABLED_SKILL_NAMES
    assert "# Available skills" in result.snapshot.system_prompt
    assert [spec.name for spec in result.snapshot.tool_specs] == [
        "skill_view",
        "fail_operation",
        "slow_operation",
        "save_value",
        "calculate_product",
        "calculate_sum",
        "record_value",
    ]
    assert "diagnostics" not in [spec.name for spec in result.snapshot.tool_specs]


async def test_offline_agent_streams_text_and_returns_full_result() -> None:
    observed = []

    result = await run(
        "hello",
        stream=True,
        on_event=observed.append,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "Starter Agent received: hello"
    assert (
        "".join(event.payload["delta"] for event in observed if event.kind == "message.delta")
        == result.text
    )
    assert [
        event.kind
        for event in observed
        if event.kind
        in {
            "model.requested",
            "message.delta",
            "model.responded",
            "message.completed",
        }
    ][0] == "model.requested"


async def test_recording_skill_composes_instructions_and_enables_tool() -> None:
    observed = []
    recording_skill = SKILL_CATALOG.resolve(VALUE_RECORDING_SKILL_NAME)

    result = await run(
        "tool=record_value calls=1",
        definition=replace(
            STARTER_AGENT_DEFINITION,
            skills=(VALUE_RECORDING_SKILL_NAME,),
            permissions=frozenset({"values:record"}),
            max_model_turns=2,
        ),
        stream=True,
        on_event=observed.append,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.snapshot.skill_names == (VALUE_RECORDING_SKILL_NAME,)
    assert "# Available skills" in result.snapshot.system_prompt
    assert "`value-recording` (version 1.0.0)" in (result.snapshot.system_prompt)
    assert recording_skill.playbook not in result.snapshot.system_prompt
    assert [spec.name for spec in result.snapshot.tool_specs] == [
        "skill_view",
        "fail_operation",
        "slow_operation",
        "save_value",
        "record_value",
    ]
    skill_snapshot = result.snapshot.skills[0]
    assert len(skill_snapshot.package_fingerprint or "") == 64
    assert skill_snapshot.provided_tools == ("record_value",)
    assert skill_snapshot.required_permissions == ()
    assert skill_snapshot.scripts == ("scripts/record.py",)
    assert recording_skill.package is not None
    assert "SKILL.md" in recording_skill.package.files
    assert "tools.py" in recording_skill.package.files
    assert result.tool_results[0].status is ToolResultStatus.SUCCESS
    assert result.tool_results[0].data == 1
    assert "model.tool_call.delta" in [event.kind for event in observed]


async def test_python_script_skill_auto_loads_tool_backed_by_script() -> None:
    result = await run(
        'tool=calculate_sum calls=1 tool_args={"left":2,"right":3}',
        definition=replace(
            STARTER_AGENT_DEFINITION,
            skills=(PYTHON_CALCULATOR_SKILL_NAME,),
            permissions=frozenset({"skills:scripts:execute"}),
            max_model_turns=2,
        ),
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.tool_results[0].status is ToolResultStatus.SUCCESS
    assert result.tool_results[0].data == {"sum": 5}
    assert result.snapshot.skill_names == ("python-calculator",)
    skill_snapshot = result.snapshot.skills[0]
    assert skill_snapshot.scripts == ("scripts/calculate.py",)
    assert skill_snapshot.provided_tools == ("calculate_product", "calculate_sum")
    assert skill_snapshot.script_runtimes == ()
    assert skill_snapshot.script_permission is None
    assert "calculate_sum" in [spec.name for spec in result.snapshot.tool_specs]


async def test_python_calculator_skill_full_chain_loads_executes_and_answers() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="load-python-calculator",
                        name="skill_view",
                        arguments={"name": "python-calculator"},
                    ),
                )
            ),
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="calculate-sum",
                        name="calculate_sum",
                        arguments={"left": 1, "right": 2},
                    ),
                )
            ),
            ModelResponse(content="According to ToolResult, 1 + 2 = 3."),
        ]
    )
    observed_events = []

    result = await build_agent(
        model,
        definition=replace(
            STARTER_AGENT_DEFINITION,
            skills=(PYTHON_CALCULATOR_SKILL_NAME,),
            permissions=frozenset({"skills:scripts:execute"}),
            max_model_turns=3,
        ),
    ).run(
        "Use the python-calculator Skill to calculate 1 + 2.",
        on_event=observed_events.append,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "According to ToolResult, 1 + 2 = 3."
    assert [tool_result.call.name for tool_result in result.tool_results] == [
        "skill_view",
        "calculate_sum",
    ]
    assert result.tool_results[1].data == {"sum": 3}
    assert len(model.requests) == 3
    assert model.requests[2].tools == ()

    skill_events = [event.kind for event in observed_events if event.kind.startswith("skill.")]
    assert skill_events == ["skill.selected", "skill.loaded", "skill.resource.used"]

    playbook_observation = json.loads(model.requests[1].messages[-1].content or "{}")
    assert "## Procedure" in playbook_observation["data"]["content"]
    calculation_observation = json.loads(model.requests[2].messages[-1].content or "{}")
    assert calculation_observation["data"] == {"sum": 3}


async def test_python_script_skill_tool_enforces_execution_permission() -> None:
    result = await run(
        'tool=calculate_sum calls=1 tool_args={"left":2,"right":3}',
        definition=replace(
            STARTER_AGENT_DEFINITION,
            skills=(PYTHON_CALCULATOR_SKILL_NAME,),
            permissions=frozenset(),
            max_model_turns=2,
        ),
    )

    assert result.tool_results[0].status is ToolResultStatus.DENIED
    assert result.tool_results[0].error == "missing permissions: skills:scripts:execute"


async def test_recording_skill_tool_enforces_permission() -> None:
    result = await run(
        'tool=record_value calls=1 tool_args={"value":1}',
        definition=replace(
            STARTER_AGENT_DEFINITION,
            skills=(VALUE_RECORDING_SKILL_NAME,),
            permissions=frozenset(),
            max_model_turns=2,
        ),
    )

    assert result.tool_results[0].status is ToolResultStatus.DENIED
    assert result.tool_results[0].error == "missing permissions: values:record"


async def test_streamed_tool_call_is_assembled_before_execution() -> None:
    observed = []

    result = await run(
        "tool=record_value calls=1",
        definition=replace(STARTER_AGENT_DEFINITION, max_model_turns=2),
        stream=True,
        on_event=observed.append,
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.tool_results[0].status is ToolResultStatus.SUCCESS
    assert result.tool_results[0].data == 1
    tool_call_deltas = [event for event in observed if event.kind == "model.tool_call.delta"]
    assert len(tool_call_deltas) == 2
    assert tool_call_deltas[0].payload["tool_call_id"] == "call-1"
    assert tool_call_deltas[0].payload["tool_name"] == "record_value"
    assert {event.turn for event in tool_call_deltas} == {1}
    assert set(tool_call_deltas[0].payload) == {
        "index",
        "tool_call_id",
        "tool_name",
        "argument_bytes",
    }
    assert sum(event.payload["argument_bytes"] for event in tool_call_deltas) == len(b'{"value":1}')
    tool_events = [event for event in observed if event.kind.startswith("tool.")]
    assert tool_events
    assert {event.turn for event in tool_events} == {1}
    assert all("turn" not in event.payload for event in tool_events)
    assert [event.kind for event in observed].index("tool.started") > [
        event.kind for event in observed
    ].index("model.tool_call.delta")


async def test_application_can_inject_a_deterministic_provider() -> None:
    model = FakeModel.text("controlled response")

    result = await build_agent(model).run("hello")

    assert result.text == "controlled response"
    assert model.requests[0].messages[0].content == "hello"


async def test_prompt_asset_is_frozen_into_the_run_snapshot() -> None:
    result = await run(
        "inspect prompt",
        definition=replace(STARTER_AGENT_DEFINITION, skills=()),
    )

    assert result.snapshot.system_prompt == (
        "Answer the user's request clearly and briefly.\n"
        "When asked to test a Tool failure, call fail_operation with the "
        "requested message.\n"
        "When asked to test a Tool timeout, call slow_operation with the "
        "requested delay.\n"
        "When asked to save a value, call save_value with the requested "
        "value."
    )
    assert len(result.snapshot.fingerprint) == 64


async def test_agent_app_prompt_triggers_the_per_turn_tool_limit() -> None:
    result = await run(
        "Call record_value once per value. calls=3",
        definition=replace(
            STARTER_AGENT_DEFINITION,
            max_tool_calls_per_turn=2,
        ),
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=2, not_found=0, denied=0, confirmation_denied=0, "
        "invalid_arguments=0, error=0, timeout=0, limit_exceeded=1"
    )
    assert [item.status for item in result.tool_results] == [
        ToolResultStatus.SUCCESS,
        ToolResultStatus.SUCCESS,
        ToolResultStatus.LIMIT_EXCEEDED,
    ]


async def test_observations_are_json_and_match_tool_calls() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="first-call",
                        name="record_value",
                        arguments={"value": 11},
                    ),
                    ToolCall(
                        id="second-call",
                        name="record_value",
                        arguments={"value": 22},
                    ),
                )
            ),
            ModelResponse(content="observed"),
        ]
    )

    result = await run(
        "record two values",
        model=model,
    )

    assert result.status is RunStatus.SUCCEEDED
    tool_messages = [message for message in model.requests[1].messages if message.role == "tool"]
    assert [
        (
            message.tool_call_id,
            message.name,
            json.loads(message.content),
        )
        for message in tool_messages
    ] == [
        (
            "first-call",
            "record_value",
            {"status": "success", "data": 11, "error": None},
        ),
        (
            "second-call",
            "record_value",
            {"status": "success", "data": 22, "error": None},
        ),
    ]


async def test_offline_provider_can_show_observation_contract() -> None:
    result = await run(
        "tool=record_value calls=2 show_observations=true",
    )

    assert result.status is RunStatus.SUCCEEDED
    assert json.loads(result.text) == {
        "observations": [
            {
                "tool_call_id": "call-1",
                "name": "record_value",
                "content": {
                    "status": "success",
                    "data": 1,
                    "error": None,
                },
            },
            {
                "tool_call_id": "call-2",
                "name": "record_value",
                "content": {
                    "status": "success",
                    "data": 2,
                    "error": None,
                },
            },
        ]
    }


async def test_agent_app_denies_tool_without_permission() -> None:
    result = await run(
        "Call record_value once. calls=1",
        definition=replace(
            STARTER_AGENT_DEFINITION,
            permissions=frozenset(),
        ),
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=0, not_found=0, denied=1, confirmation_denied=0, "
        "invalid_arguments=0, error=0, timeout=0, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.DENIED
    assert result.tool_results[0].data is None
    assert "missing permissions: values:record" in (result.tool_results[0].error or "")
    assert "tool.started" not in [event.kind for event in result.events]
    completed = next(event for event in result.events if event.kind == "tool.completed")
    assert completed.turn == 1
    assert "turn" not in completed.payload


async def test_agent_app_executes_tool_with_granted_permission() -> None:
    result = await run(
        "Call record_value once. calls=1",
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=1, not_found=0, denied=0, confirmation_denied=0, "
        "invalid_arguments=0, error=0, timeout=0, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.SUCCESS
    assert result.tool_results[0].data == 1
    assert result.snapshot.permissions == ENABLED_PERMISSIONS
    assert "tool.started" in [event.kind for event in result.events]


async def test_agent_app_rejects_invalid_tool_arguments_before_execution() -> None:
    prompts = (
        'Call record_value once. calls=1 tool_args={"value":"abc"}',
        "Call record_value once. calls=1 tool_args={}",
        'Call record_value once. calls=1 tool_args={"value":1,"extra":true}',
    )

    for prompt_text in prompts:
        result = await run(
            prompt_text,
        )

        assert result.status is RunStatus.SUCCEEDED
        assert result.text == (
            "executed=0, not_found=0, denied=0, "
            "confirmation_denied=0, "
            "invalid_arguments=1, error=0, timeout=0, "
            "limit_exceeded=0"
        )
        assert result.tool_results[0].status is ToolResultStatus.INVALID_ARGUMENTS
        assert result.tool_results[0].data is None
        assert result.tool_results[0].error
        assert "tool.started" not in [event.kind for event in result.events]


async def test_record_value_schema_is_exposed_to_the_provider() -> None:
    result = await run("inspect schema")
    schema = next(
        spec.input_schema
        for spec in result.snapshot.tool_specs
        if spec.name == "record_value"
    )

    assert schema["properties"]["value"]["type"] == "integer"
    assert schema["required"] == ["value"]
    assert schema["additionalProperties"] is False


async def test_agent_app_converts_tool_exception_into_error_observation() -> None:
    result = await run(
        'tool=fail_operation calls=1 tool_args={"message":"expected failure"}',
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=0, not_found=0, denied=0, confirmation_denied=0, "
        "invalid_arguments=0, error=1, timeout=0, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.ERROR
    assert result.tool_results[0].data is None
    assert result.tool_results[0].error == ("RuntimeError: expected failure")
    tool_events = [
        event for event in result.events if event.kind in {"tool.started", "tool.completed"}
    ]
    assert [event.kind for event in tool_events] == [
        "tool.started",
        "tool.completed",
    ]
    assert tool_events[-1].payload["status"] == "error"


async def test_agent_app_converts_tool_timeout_into_observation() -> None:
    result = await run(
        'tool=slow_operation calls=1 tool_args={"delay_seconds":0.2}',
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=0, not_found=0, denied=0, confirmation_denied=0, "
        "invalid_arguments=0, error=0, timeout=1, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.TIMEOUT
    assert result.tool_results[0].data is None
    assert result.tool_results[0].error == ("Tool exceeded 0.05s timeout")
    tool_events = [
        event for event in result.events if event.kind in {"tool.started", "tool.completed"}
    ]
    assert [event.kind for event in tool_events] == [
        "tool.started",
        "tool.completed",
    ]
    assert tool_events[-1].payload["status"] == "timeout"


async def test_agent_app_denies_unconfirmed_side_effect() -> None:
    result = await run(
        'tool=save_value calls=1 tool_args={"value":7}',
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=0, not_found=0, denied=0, confirmation_denied=1, "
        "invalid_arguments=0, error=0, timeout=0, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.CONFIRMATION_DENIED
    assert result.tool_results[0].data is None
    assert result.tool_results[0].error == ("Tool execution was not confirmed")
    events = result.events
    confirmation = next(event for event in events if event.kind == "tool.confirmation")
    assert confirmation.turn == 1
    assert "turn" not in confirmation.payload
    assert confirmation.payload["approved"] is False
    assert "tool.started" not in [event.kind for event in events]


async def test_agent_app_executes_explicitly_confirmed_side_effect() -> None:
    result = await run(
        'tool=save_value calls=1 tool_args={"value":7}',
        confirmation_policy=AllowAllConfirmations(),
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=1, not_found=0, denied=0, confirmation_denied=0, "
        "invalid_arguments=0, error=0, timeout=0, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.SUCCESS
    assert result.tool_results[0].data == 7
    events = result.events
    confirmation = next(event for event in events if event.kind == "tool.confirmation")
    assert confirmation.turn == 1
    assert confirmation.payload["approved"] is True
    assert "tool.started" in [event.kind for event in events]
    completed = [event for event in events if event.kind == "tool.completed"]
    assert {event.turn for event in completed} == {1}
    assert completed[-1].payload["status"] == "success"


async def test_registered_but_disabled_tool_is_not_found(
    monkeypatch,
) -> None:
    monkeypatch.delenv("STARTER_BROWSER_EXECUTABLE", raising=False)

    result = await run(
        'tool=browse calls=1 tool_args={"url":"https://example.com"}',
    )

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == (
        "executed=0, not_found=1, denied=0, confirmation_denied=0, "
        "invalid_arguments=0, error=0, timeout=0, limit_exceeded=0"
    )
    assert result.tool_results[0].status is ToolResultStatus.NOT_FOUND
    assert result.tool_results[0].data is None
    assert result.tool_results[0].error == ("Tool 'browse' is not enabled for this Agent")
    assert "browse" not in [spec.name for spec in result.snapshot.tool_specs]
    event_kinds = [event.kind for event in result.events]
    assert "tool.not_ready" not in event_kinds
    assert "tool.started" not in event_kinds
    completed = [event for event in result.events if event.kind == "tool.completed"]
    assert completed[-1].payload["status"] == "not_found"


def test_browser_readiness_directly_reports_missing_configuration(
    monkeypatch,
) -> None:
    monkeypatch.delenv("STARTER_BROWSER_EXECUTABLE", raising=False)

    readiness = check_browser_readiness()

    assert readiness.ready is False
    assert readiness.detail == "STARTER_BROWSER_EXECUTABLE is not configured"


async def test_opt_in_browser_tool_fails_before_model_when_not_ready(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "STARTER_BROWSER_EXECUTABLE",
        "definitely-missing-starter-browser",
    )
    monkeypatch.setattr("agent_app.tools.shutil.which", lambda _: None)
    model = FakeModel.text("must not be called")

    result = await build_agent(
        model,
        definition=replace(
            STARTER_AGENT_DEFINITION,
            tools=(*STARTER_AGENT_DEFINITION.tools, "browse"),
        ),
    ).run("test browser readiness")

    assert result.status is RunStatus.FAILED
    assert result.error == (
        "Tool 'browse' is not ready: browser executable "
        "'definitely-missing-starter-browser' was not found"
    )
    assert model.requests == ()
    assert result.tool_results == ()
    not_ready = [event for event in result.events if event.kind == "tool.not_ready"]
    assert len(not_ready) == 1
    assert not_ready[0].payload["tool_name"] == "browse"


async def test_opt_in_browser_tool_allows_run_when_ready(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "STARTER_BROWSER_EXECUTABLE",
        "starter-browser",
    )
    monkeypatch.setattr(
        "agent_app.tools.shutil.which",
        lambda _: "/opt/starter-browser",
    )
    model = FakeModel.text("browser dependency is ready")

    result = await build_agent(
        model,
        definition=replace(
            STARTER_AGENT_DEFINITION,
            tools=(*STARTER_AGENT_DEFINITION.tools, "browse"),
        ),
    ).run("test browser readiness")

    assert result.status is RunStatus.SUCCEEDED
    assert result.text == "browser dependency is ready"
    assert len(model.requests) == 1
    assert "run.ready" in [event.kind for event in result.events]
    assert "tool.not_ready" not in [event.kind for event in result.events]


def test_provider_factory_defaults_to_offline() -> None:
    assert isinstance(build_provider(), OfflineProvider)


def test_openai_compatible_provider_reads_environment(
    monkeypatch,
    tmp_path,
) -> None:
    captured: dict[str, object] = {}

    class CapturingProvider:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        @property
        def info(self) -> ProviderInfo:
            raise AssertionError("not used by this configuration test")

    monkeypatch.setenv("OPENAI_API_KEY", "secret-for-test")
    monkeypatch.setenv("OPENAI_MODEL", "served-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://localhost:8000/v1")
    monkeypatch.setattr("agent_app.config._DOTENV_PATH", tmp_path / ".env")
    monkeypatch.setattr(
        "agent_app.provider.OpenAIChatProvider",
        CapturingProvider,
    )

    provider = build_provider(Settings.from_env(provider="openai-compatible"))

    assert isinstance(provider, CapturingProvider)
    assert captured == {
        "default_model": "served-model",
        "api_key": "secret-for-test",
        "base_url": "http://localhost:8000/v1",
        "timeout_seconds": 60,
        "structured_output_mode": "prompt",
    }


def test_openai_compatible_provider_requires_model(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-for-test")
    monkeypatch.delenv("AGENT_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.setattr("agent_app.config._DOTENV_PATH", tmp_path / ".env")

    try:
        build_provider(Settings.from_env(provider="openai-compatible"))
    except ValueError as exc:
        assert str(exc) == ("OpenAI-compatible mode requires --model, AGENT_MODEL, or OPENAI_MODEL")
    else:
        raise AssertionError("missing model must fail")


def test_cli_provider_modes_preserve_local_default_model(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingProvider:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        @property
        def info(self) -> ProviderInfo:
            raise AssertionError("not used by this configuration test")

    monkeypatch.setattr(
        "agent_app.provider.CodexCLIProvider",
        CapturingProvider,
    )

    provider = build_provider(
        Settings(
            provider="codex-cli",
            cli_executable="/trusted/codex",
            cli_timeout_seconds=12,
            cli_max_output_bytes=1234,
        )
    )

    assert isinstance(provider, CapturingProvider)
    assert captured == {
        "model": None,
        "executable": "/trusted/codex",
        "timeout_seconds": 12,
        "max_output_bytes": 1234,
    }


def test_plan_react_provider_factory_uses_independent_endpoints(monkeypatch) -> None:
    captured: list[dict[str, object]] = []

    class CapturingProvider:
        def __init__(self, **kwargs: object) -> None:
            captured.append(dict(kwargs))

        @property
        def info(self) -> ProviderInfo:
            raise AssertionError("not used by this configuration test")

    monkeypatch.setattr(
        "agent_app.provider.OpenAIChatProvider",
        CapturingProvider,
    )

    planner, executor = build_plan_react_providers(
        Settings(
            provider="openai-compatible",
            model="default-model",
            api_key="default-key",
            base_url="http://default.local/v1",
            plan_react_planner_model="planner-model",
            plan_react_planner_api_key="planner-key",
            plan_react_planner_base_url="http://planner.local/v1",
            plan_react_executor_model="executor-model",
            plan_react_executor_api_key="executor-key",
            plan_react_executor_base_url="http://executor.local/v1",
        )
    )

    assert planner is not executor
    assert captured == [
        {
            "default_model": "planner-model",
            "api_key": "planner-key",
            "base_url": "http://planner.local/v1",
            "timeout_seconds": 60,
            "structured_output_mode": "prompt",
        },
        {
            "default_model": "executor-model",
            "api_key": "executor-key",
            "base_url": "http://executor.local/v1",
            "timeout_seconds": 60,
            "structured_output_mode": "prompt",
        },
    ]


def test_plan_react_provider_factory_reuses_the_default_route() -> None:
    default = OfflineProvider()

    planner, executor = build_plan_react_providers(
        Settings(provider="offline"),
        default_provider=default,
    )

    assert planner is default
    assert executor is default
