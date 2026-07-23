"""Application Tools: small, typed, permissioned, and independently testable."""

from base_agent import tool


@tool(permissions=frozenset({"text:analyze"}))
async def word_count(text: str) -> dict[str, int]:
    """Count Unicode whitespace-delimited words and characters in text."""
    return {"words": len(text.split()), "characters": len(text)}


TOOLS = (word_count,)
