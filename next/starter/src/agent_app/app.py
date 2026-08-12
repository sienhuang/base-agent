"""The application's single Agent definition and composition root."""

from base_agent import Agent, AgentDefinition, RunResult
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
from agent_app.tools import (
    ENABLED_TOOL_NAMES,
    REGISTERED_TOOLS,
)

ENABLED_SKILL_NAMES = (
    PYTHON_CALCULATOR_SKILL_NAME,
    VALUE_RECORDING_SKILL_NAME,
)
ENABLED_PERMISSIONS = frozenset(
    {
        "skills:scripts:execute",
        "values:record",
        "values:write",
    }
)

STARTER_AGENT_DEFINITION = AgentDefinition(
    id="starter-assistant",
    version="1.0.0",
    description="A minimal Starter Agent with typed Tools and Skills.",
    instructions=prompt("assistant.md"),
    tools=ENABLED_TOOL_NAMES,
    skills=ENABLED_SKILL_NAMES,
    permissions=ENABLED_PERMISSIONS,
    max_model_turns=10,
    max_tool_calls_per_turn=3,
)


def build_agent(
    model: ModelProvider | None = None,
    *,
    definition: AgentDefinition = STARTER_AGENT_DEFINITION,
    confirmation_policy: ConfirmationPolicy | None = None,
) -> Agent:
    """Compose application Prompt and Provider with the SDK Agent facade."""
    return Agent(
        definition,
        model=model or OfflineProvider(),
        tools=REGISTERED_TOOLS,
        skills=SKILL_CATALOG,
        confirmation_policy=confirmation_policy,
    )


async def run(
    prompt_text: str,
    *,
    model: ModelProvider | None = None,
    definition: AgentDefinition = STARTER_AGENT_DEFINITION,
    confirmation_policy: ConfirmationPolicy | None = None,
    stream: bool = False,
    on_event: Observer | None = None,
) -> RunResult:
    agent = build_agent(
        model,
        definition=definition,
        confirmation_policy=confirmation_policy,
    )
    if stream:
        if on_event is None:
            raise ValueError("streaming run requires an on_event observer")
        return await agent.stream(prompt_text, on_event=on_event)
    return await agent.run(prompt_text, on_event=on_event)
