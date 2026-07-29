"""Human-in-the-loop tools shared by agent applications."""

from typing import Any

from base_agent.models import WaitForInput
from base_agent.tools import FunctionTool, tool


def interaction_tools() -> tuple[FunctionTool, ...]:
    """Build tools that can suspend a Run while waiting for user input."""

    @tool(name="ask_user", permissions=frozenset({"interaction:ask"}))
    async def ask_user(
        question: str,
        metadata: dict[str, Any] | None = None,
    ) -> WaitForInput:
        """Ask the user a necessary question and resume this Run with their answer."""
        return WaitForInput(prompt=question, metadata=metadata or {})

    return (ask_user,)
