"""Convenience composition for the dependency-free built-in ToolKit."""

from pathlib import Path

from base_agent.toolkits.artifacts import artifact_tools
from base_agent.toolkits.interaction import interaction_tools
from base_agent.toolkits.memory import memory_tools
from base_agent.toolkits.utility import utility_tools
from base_agent.toolkits.workspace import workspace_tools
from base_agent.tools import FunctionTool


def basic_tools(
    *,
    workspace_root: str | Path | None = None,
    include_interaction: bool = True,
    include_artifacts: bool = True,
    include_memory: bool = True,
    include_utilities: bool = True,
) -> tuple[FunctionTool, ...]:
    """Compose the safe, dependency-free ToolKit for a new Agent application."""
    tools: list[FunctionTool] = []
    if include_interaction:
        tools.extend(interaction_tools())
    if include_artifacts:
        tools.extend(artifact_tools())
    if include_memory:
        tools.extend(memory_tools())
    if include_utilities:
        tools.extend(utility_tools())
    if workspace_root is not None:
        tools.extend(workspace_tools(workspace_root))
    return tuple(tools)
