"""The application's Agent and Flow definitions and composition root."""

from collections.abc import Sequence
from dataclasses import replace

from base_agent import (
    Agent,
    AgentDefinition,
    ArtifactStore,
    CheckpointStore,
    RunResult,
)
from base_agent.context import ContextBudget, ContextPolicy
from base_agent.events import EventStore
from base_agent.flows import (
    Flow,
    PlannerDecision,
    PlannerReActFlow,
    PlannerReActResult,
    PlanningLimits,
    StepOutcome,
)
from base_agent.flows.checkpoints import CheckpointWriter
from base_agent.governance import ConfirmationPolicy
from base_agent.observability import Observer
from base_agent.providers import ModelProvider
from base_agent.tools import Tool, Toolkit, flatten_tools

from agent_app.assets import prompt
from agent_app.context import RecentToolGroupsContextPolicy
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
        "interaction:ask",
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

PLAN_REACT_PLANNER_DEFINITION = AgentDefinition(
    id="starter-planner",
    version="1.0.0",
    description="A fixed Planner that creates and reviews Host-owned Plans.",
    instructions=prompt("planner.md"),
    response_model=PlannerDecision,
    max_model_turns=1,
)

PLAN_REACT_EXECUTOR_DEFINITION = replace(
    STARTER_AGENT_DEFINITION,
    id="starter-executor",
    description="A ReAct Executor that completes one Host-selected Plan Step.",
    instructions=prompt("executor.md"),
    response_model=StepOutcome,
)


def build_agent(
    model: ModelProvider | None = None,
    *,
    definition: AgentDefinition = STARTER_AGENT_DEFINITION,
    confirmation_policy: ConfirmationPolicy | None = None,
    observers: Sequence[Observer] = (),
    artifact_store: ArtifactStore | None = None,
    event_store: EventStore | None = None,
    checkpoint_store: CheckpointStore | None = None,
    context_policy: ContextPolicy | None = None,
    context_budget: ContextBudget | None = None,
    additional_tools: Sequence[Tool | Toolkit] = (),
) -> Agent:
    """Compose application Prompt and Provider with the SDK Agent facade."""
    extra_tools = flatten_tools(additional_tools)
    extra_names = tuple(tool.spec.name for tool in extra_tools)
    extra_permissions = frozenset(
        permission for tool in extra_tools for permission in tool.permissions
    )
    resolved_definition = (
        replace(
            definition,
            tools=tuple(dict.fromkeys((*definition.tools, *extra_names))),
            permissions=definition.permissions | extra_permissions,
        )
        if extra_tools
        else definition
    )
    return Agent(
        resolved_definition,
        model=model or OfflineProvider(),
        tools=(*REGISTERED_TOOLS, *extra_tools),
        skills=SKILL_CATALOG,
        confirmation_policy=confirmation_policy,
        context_policy=context_policy,
        context_budget=context_budget,
        observers=observers,
        artifact_store=artifact_store,
        event_store=event_store,
        checkpoint_store=checkpoint_store,
    )


def build_plan_react_flow(
    model: ModelProvider | None = None,
    *,
    planner_provider: ModelProvider | None = None,
    executor_provider: ModelProvider | None = None,
    planner_model: str | None = None,
    executor_model: str | None = None,
    confirmation_policy: ConfirmationPolicy | None = None,
    limits: PlanningLimits | None = None,
    observers: Sequence[Observer] = (),
    artifact_store: ArtifactStore,
    event_store: EventStore | None = None,
    checkpoint_store: CheckpointStore | None = None,
    flow_checkpoint_writer: CheckpointWriter | None = None,
    planner_context_policy: ContextPolicy | None = None,
    planner_context_budget: ContextBudget | None = None,
    executor_context_policy: ContextPolicy | None = None,
    executor_context_budget: ContextBudget | None = None,
    additional_tools: Sequence[Tool | Toolkit] = (),
) -> PlannerReActFlow:
    """Compose Planner and Executor Agents with independently selectable Providers."""
    provider = model or OfflineProvider()
    planner_definition = (
        PLAN_REACT_PLANNER_DEFINITION
        if planner_model is None
        else replace(PLAN_REACT_PLANNER_DEFINITION, model=planner_model)
    )
    executor_definition = (
        PLAN_REACT_EXECUTOR_DEFINITION
        if executor_model is None
        else replace(PLAN_REACT_EXECUTOR_DEFINITION, model=executor_model)
    )
    planner = Agent(
        planner_definition,
        model=planner_provider or provider,
        context_policy=planner_context_policy,
        context_budget=planner_context_budget,
        artifact_store=artifact_store,
        event_store=event_store,
        checkpoint_store=checkpoint_store,
    )
    executor = build_agent(
        executor_provider or provider,
        definition=executor_definition,
        confirmation_policy=confirmation_policy,
        context_policy=(
            executor_context_policy
            if executor_context_policy is not None
            else RecentToolGroupsContextPolicy()
        ),
        context_budget=executor_context_budget,
        artifact_store=artifact_store,
        event_store=event_store,
        checkpoint_store=checkpoint_store,
        additional_tools=additional_tools,
    )
    return Flow.planner_react(
        id="starter-plan-react",
        planner=planner,
        executor=executor,
        limits=limits,
        observers=observers,
        forward_child_events=True,
        event_store=event_store,
        checkpoint_writer=flow_checkpoint_writer,
    )


async def run(
    prompt_text: str,
    *,
    model: ModelProvider | None = None,
    definition: AgentDefinition = STARTER_AGENT_DEFINITION,
    confirmation_policy: ConfirmationPolicy | None = None,
    stream: bool = False,
    on_event: Observer | None = None,
    artifact_store: ArtifactStore | None = None,
    event_store: EventStore | None = None,
    checkpoint_store: CheckpointStore | None = None,
    context_policy: ContextPolicy | None = None,
    context_budget: ContextBudget | None = None,
    additional_tools: Sequence[Tool | Toolkit] = (),
) -> RunResult:
    agent = build_agent(
        model,
        definition=definition,
        confirmation_policy=confirmation_policy,
        artifact_store=artifact_store,
        event_store=event_store,
        checkpoint_store=checkpoint_store,
        context_policy=context_policy,
        context_budget=context_budget,
        additional_tools=additional_tools,
    )
    if stream:
        if on_event is None:
            raise ValueError("streaming run requires an on_event observer")
        return await agent.stream(prompt_text, on_event=on_event)
    return await agent.run(prompt_text, on_event=on_event)


async def run_plan_react(
    prompt_text: str,
    *,
    model: ModelProvider | None = None,
    planner_provider: ModelProvider | None = None,
    executor_provider: ModelProvider | None = None,
    planner_model: str | None = None,
    executor_model: str | None = None,
    confirmation_policy: ConfirmationPolicy | None = None,
    limits: PlanningLimits | None = None,
    stream: bool = False,
    on_event: Observer | None = None,
    artifact_store: ArtifactStore,
    event_store: EventStore | None = None,
    checkpoint_store: CheckpointStore | None = None,
    planner_context_policy: ContextPolicy | None = None,
    planner_context_budget: ContextBudget | None = None,
    executor_context_policy: ContextPolicy | None = None,
    executor_context_budget: ContextBudget | None = None,
) -> PlannerReActResult:
    """Run the Starter's fixed Planner/ReAct composition to completion."""
    observers = () if on_event is None else (on_event,)
    flow = build_plan_react_flow(
        model,
        planner_provider=planner_provider,
        executor_provider=executor_provider,
        planner_model=planner_model,
        executor_model=executor_model,
        confirmation_policy=confirmation_policy,
        limits=limits,
        observers=observers,
        artifact_store=artifact_store,
        event_store=event_store,
        checkpoint_store=checkpoint_store,
        planner_context_policy=planner_context_policy,
        planner_context_budget=planner_context_budget,
        executor_context_policy=executor_context_policy,
        executor_context_budget=executor_context_budget,
    )
    return await flow.run(prompt_text, stream=stream)
