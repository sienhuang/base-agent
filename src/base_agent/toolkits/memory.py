"""Explicit retrieval tools over the Runtime's configured memory boundary."""

from typing import Any

from base_agent.models import MemoryQuery
from base_agent.tools import FunctionTool, ToolContext, tool


def memory_tools() -> tuple[FunctionTool, ...]:
    """Build an opt-in memory search tool."""

    @tool(name="search_memory", permissions=frozenset({"memory:read"}))
    async def search_memory(
        query: str,
        context: ToolContext,
        limit: int = 5,
        namespace: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, object]]:
        """Search configured long-term memory when the current task needs prior context."""
        matches = await context.memories.search(
            MemoryQuery(
                text=query,
                limit=limit,
                namespace=namespace,
                run_id=context.run_id,
                filters=filters or {},
            )
        )
        return [
            {
                "score": match.score,
                "record": match.record.model_dump(mode="json"),
            }
            for match in matches
        ]

    return (search_memory,)
