"""Human-in-the-loop Tools shared by Agent applications."""

from typing import Any

from base_agent.models import WaitForInput
from base_agent.tools import FunctionTool, tool


def interaction_tools() -> tuple[FunctionTool, ...]:
    """Build Tools that let the model request necessary user input."""

    @tool(name="ask_user", permissions=frozenset({"interaction:ask"}))
    async def ask_user(
        question: str,
        metadata: dict[str, Any] | None = None,
    ) -> WaitForInput:
        """Request information required to continue the current task.

        Call this Tool instead of asking the user in a normal assistant message.
        If an answer is incomplete, call this Tool again with one focused question.
        """
        return WaitForInput(prompt=question, metadata=metadata or {})

    return (ask_user,)
