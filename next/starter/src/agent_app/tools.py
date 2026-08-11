"""Application-owned Tools introduced in Starter Step 02."""

import asyncio
import os
import shutil

from base_agent import (
    Confirmation,
    SideEffect,
    ToolReadiness,
    tool,
)


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
