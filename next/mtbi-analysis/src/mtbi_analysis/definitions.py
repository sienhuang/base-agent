"""Agent definitions and composition for the MTBI application."""

from collections.abc import Iterable
from dataclasses import replace

from base_agent import (
    Agent,
    AgentDefinition,
    ArtifactStore,
    ResourceSpec,
    ToolChoice,
    Toolkit,
)
from base_agent.providers import ModelProvider

from mtbi_analysis.assets import prompt
from mtbi_analysis.models import MTBIAnalysisOutcome, MTBIQueryOutcome

MTBI_QUERY_AGENT_DEFINITION = AgentDefinition(
    id="mtbi-query-agent",
    version="1.0.0",
    description="Discovers MTBI metadata and produces one checked query dataset.",
    instructions=prompt("query.md"),
    tools=("mtbi_cli", "write_workspace_text"),
    permissions=frozenset({"workspace:write"}),
    tool_choice=ToolChoice.REQUIRED,
    response_model=MTBIQueryOutcome,
    max_model_turns=12,
    max_tool_calls_per_turn=3,
)


MTBI_ANALYSIS_AGENT_DEFINITION = AgentDefinition(
    id="mtbi-data-analysis-agent",
    version="1.0.0",
    description="Analyzes one Host-verified CSV and writes a Markdown report.",
    instructions=prompt("analysis.md"),
    tools=("summarize_csv", "write_workspace_text"),
    permissions=frozenset({"workspace:write"}),
    tool_choice=ToolChoice.REQUIRED,
    response_model=MTBIAnalysisOutcome,
    max_model_turns=8,
    max_tool_calls_per_turn=2,
)


def build_agents(
    provider: ModelProvider,
    toolkit: Toolkit,
    *,
    artifact_store: ArtifactStore,
    resources: Iterable[ResourceSpec],
    max_tool_observation_bytes: int,
    max_run_observation_bytes: int,
) -> tuple[Agent, Agent]:
    resource_specs = tuple(resources)
    query_definition = replace(
        MTBI_QUERY_AGENT_DEFINITION,
        max_tool_observation_bytes=max_tool_observation_bytes,
        max_run_observation_bytes=max_run_observation_bytes,
    )
    analysis_definition = replace(
        MTBI_ANALYSIS_AGENT_DEFINITION,
        max_tool_observation_bytes=max_tool_observation_bytes,
        max_run_observation_bytes=max_run_observation_bytes,
    )
    return (
        Agent(
            query_definition,
            model=provider,
            tools=(toolkit,),
            artifact_store=artifact_store,
            resources=resource_specs,
        ),
        Agent(
            analysis_definition,
            model=provider,
            tools=(toolkit,),
            artifact_store=artifact_store,
            resources=resource_specs,
        ),
    )
