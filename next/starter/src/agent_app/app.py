"""The application's one composition root."""

from base_agent import Agent, AgentDefinition, RunResult, Toolkit
from base_agent.governance import ConfirmationPolicy
from base_agent.observability import Observer
from base_agent.providers import ModelProvider

from agent_app.assets import prompt
from agent_app.provider import OfflineProvider
from agent_app.skills import (
    PYTHON_CALCULATOR_SKILL_NAME,
    SKILL_CATALOG,
    VALUE_RECORDING_SKILL_NAME,
)
from agent_app.skills.value_recording.tools import record_value
from agent_app.tools import (
    browse,
    fail_operation,
    save_value,
    slow_operation,
)

DIAGNOSTIC_TOOLKIT = Toolkit.of(
    "diagnostics",
    fail_operation,
    slow_operation,
)


def build_agent(
    model: ModelProvider | None = None,
    *,
    permissions: frozenset[str] = frozenset(),
    max_model_turns: int = 10,
    max_tool_calls_per_turn: int = 3,
    confirmation_policy: ConfirmationPolicy | None = None,
    enable_browser_tool: bool = False,
    enable_recording_skill: bool = False,
    enable_python_script_skill: bool = False,
) -> Agent:
    """Compose application Prompt and Provider with the SDK Agent facade."""
    enabled_tool_names: tuple[str, ...] = (
        "fail_operation",
        "slow_operation",
        "save_value",
    )
    enabled_skills: tuple[str, ...] = ()
    if enable_recording_skill:
        enabled_skills = (VALUE_RECORDING_SKILL_NAME,)
    else:
        enabled_tool_names = ("record_value", *enabled_tool_names)
    if enable_python_script_skill:
        enabled_skills += (PYTHON_CALCULATOR_SKILL_NAME,)
    if enable_browser_tool:
        enabled_tool_names += ("browse",)
    registered_tools = (
        (DIAGNOSTIC_TOOLKIT, save_value, browse)
        if enable_recording_skill
        else (record_value, DIAGNOSTIC_TOOLKIT, save_value, browse)
    )
    return Agent(
        AgentDefinition(
            id="starter-assistant",
            version="1.0.0",
            description="A minimal Starter Agent with typed Tools.",
            instructions=prompt("assistant.md"),
            tools=enabled_tool_names,
            skills=enabled_skills,
            permissions=permissions,
            max_model_turns=max_model_turns,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
        ),
        model=model or OfflineProvider(),
        tools=registered_tools,
        skills=SKILL_CATALOG,
        confirmation_policy=confirmation_policy,
    )


async def run(
    prompt_text: str,
    *,
    model: ModelProvider | None = None,
    permissions: frozenset[str] = frozenset(),
    max_model_turns: int = 10,
    max_tool_calls_per_turn: int = 3,
    confirmation_policy: ConfirmationPolicy | None = None,
    enable_browser_tool: bool = False,
    enable_recording_skill: bool = False,
    enable_python_script_skill: bool = False,
    stream: bool = False,
    on_event: Observer | None = None,
) -> RunResult:
    agent = build_agent(
        model,
        permissions=permissions,
        max_model_turns=max_model_turns,
        max_tool_calls_per_turn=max_tool_calls_per_turn,
        confirmation_policy=confirmation_policy,
        enable_browser_tool=enable_browser_tool,
        enable_recording_skill=enable_recording_skill,
        enable_python_script_skill=enable_python_script_skill,
    )
    if stream:
        if on_event is None:
            raise ValueError("streaming run requires an on_event observer")
        return await agent.stream(prompt_text, on_event=on_event)
    return await agent.run(prompt_text, on_event=on_event)
