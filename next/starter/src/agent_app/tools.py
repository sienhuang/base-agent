"""Application-owned Tools introduced in Starter Step 02."""

import asyncio
import os
import shutil

from base_agent import (
    Confirmation,
    SideEffect,
    Toolkit,
    ToolReadiness,
    interaction_tools,
    tool,
)
from base_agent.tools import Tool, flatten_tools


def check_browser_readiness() -> ToolReadiness:
    """Check the configured browser executable before a Run starts."""
    executable = os.getenv("STARTER_BROWSER_EXECUTABLE", "").strip()
    if not executable:
        return ToolReadiness(
            ready=False,
            detail="STARTER_BROWSER_EXECUTABLE is not configured",
        )
    if shutil.which(executable) is None:
        return ToolReadiness(
            ready=False,
            detail=f"browser executable '{executable}' was not found",
        )
    return ToolReadiness(ready=True)


@tool
async def fail_operation(message: str) -> str:
    """Raise a controlled exception so Harness error handling is visible."""
    raise RuntimeError(message)


@tool(timeout_seconds=0.05)
async def slow_operation(delay_seconds: float) -> str:
    """Sleep long enough to make Harness timeout handling visible."""
    await asyncio.sleep(delay_seconds)
    return f"completed after {delay_seconds:g}s"


@tool(
    permissions=frozenset({"values:write"}),
    side_effect=SideEffect.MUTATING,
    confirmation=Confirmation.REQUIRED,
)
async def save_value(value: int) -> int:
    """Represent a mutating operation that requires per-run approval."""
    return value


@tool(readiness=check_browser_readiness)
async def browse(url: str) -> str:
    """Represent a browser-backed Tool with an external dependency."""
    return f"Browser is ready for {url}"


DIAGNOSTIC_TOOLKIT = Toolkit.of(
    "diagnostics",
    fail_operation,
    slow_operation,
)

INTERACTION_TOOLS = interaction_tools()

# Registration, enablement, and authorization are intentionally separate. The
# browser implementation is registered for opt-in definitions but is not part
# of the Starter Agent's default capability policy.
REGISTERED_TOOLS: tuple[Tool | Toolkit, ...] = (
    *INTERACTION_TOOLS,
    DIAGNOSTIC_TOOLKIT,
    save_value,
    browse,
)
ENABLED_TOOLS: tuple[Tool | Toolkit, ...] = (
    *INTERACTION_TOOLS,
    DIAGNOSTIC_TOOLKIT,
    save_value,
)
ENABLED_TOOL_NAMES = tuple(tool.spec.name for tool in flatten_tools(ENABLED_TOOLS))
