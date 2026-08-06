"""Tool authoring, registration, and execution APIs."""

from base_agent.models import ToolConfirmation, ToolConfirmationDecision
from base_agent.tools.context import ToolContext
from base_agent.tools.decorator import FunctionTool, tool
from base_agent.tools.effects import (
    ConfirmableTool,
    DeclaredToolConfirmationPolicy,
    GovernedTool,
    ToolConfirmationMode,
    ToolConfirmationPolicy,
    ToolConfirmationRequest,
    ToolSideEffectContextError,
    ToolSideEffectMode,
    ToolSideEffectReceipt,
    ToolSideEffectRecorder,
    ToolSideEffectRecorderError,
    ToolSideEffectReplayUnsafeError,
)
from base_agent.tools.errors import ToolInvalidArgumentsError
from base_agent.tools.executor import ToolExecutor
from base_agent.tools.protocol import ArgumentValidatingTool, ContextualTool, Tool
from base_agent.tools.registry import DuplicateToolError, ToolNotFoundError, ToolRegistry
from base_agent.tools.results import (
    BoundedToolResultPolicy,
    ToolResultLimits,
    ToolResultPolicy,
)

__all__ = [
    "DuplicateToolError",
    "ArgumentValidatingTool",
    "BoundedToolResultPolicy",
    "ConfirmableTool",
    "DeclaredToolConfirmationPolicy",
    "FunctionTool",
    "GovernedTool",
    "Tool",
    "ContextualTool",
    "ToolContext",
    "ToolConfirmation",
    "ToolConfirmationDecision",
    "ToolConfirmationMode",
    "ToolConfirmationPolicy",
    "ToolConfirmationRequest",
    "ToolExecutor",
    "ToolInvalidArgumentsError",
    "ToolNotFoundError",
    "ToolRegistry",
    "ToolResultLimits",
    "ToolResultPolicy",
    "ToolSideEffectContextError",
    "ToolSideEffectMode",
    "ToolSideEffectReceipt",
    "ToolSideEffectRecorder",
    "ToolSideEffectRecorderError",
    "ToolSideEffectReplayUnsafeError",
    "tool",
]
