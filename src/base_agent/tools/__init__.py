"""Tool authoring, registration, and execution APIs."""

from base_agent.tools.context import ToolContext
from base_agent.tools.decorator import FunctionTool, tool
from base_agent.tools.executor import ToolExecutor
from base_agent.tools.protocol import ContextualTool, Tool
from base_agent.tools.registry import DuplicateToolError, ToolNotFoundError, ToolRegistry

__all__ = [
    "DuplicateToolError",
    "FunctionTool",
    "Tool",
    "ContextualTool",
    "ToolContext",
    "ToolExecutor",
    "ToolNotFoundError",
    "ToolRegistry",
    "tool",
]
