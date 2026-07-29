"""Application Tools plus the safe, built-in base-agent ToolKit."""

from pathlib import Path

from base_agent import basic_tools, tool

PROJECT_ROOT = Path(__file__).parents[2]


@tool(permissions=frozenset({"text:analyze"}))
async def word_count(text: str) -> dict[str, int]:
    """Count Unicode whitespace-delimited words and characters in text."""
    return {"words": len(text.split()), "characters": len(text)}


BUILTIN_TOOLS = basic_tools(workspace_root=PROJECT_ROOT)
REGISTERED_TOOLS = (word_count, *BUILTIN_TOOLS)

# The write Tool is registered so applications can opt in without rebuilding the
# ToolKit. The Starter profile deliberately exposes only bounded reads and Run-owned
# Artifact creation.
_DISABLED_BY_DEFAULT = frozenset({"workspace_write_text", "search_memory"})
ENABLED_TOOLS = tuple(
    candidate
    for candidate in REGISTERED_TOOLS
    if candidate.definition.name not in _DISABLED_BY_DEFAULT
)
ENABLED_TOOL_NAMES = tuple(candidate.definition.name for candidate in ENABLED_TOOLS)

ENABLED_PERMISSIONS = frozenset(
    {
        "text:analyze",
        "interaction:ask",
        "workspace:read",
        "artifact:read",
        "artifact:write",
    }
)

# Backward-compatible name used by the Starter's focused Tool/Skill tests.
TOOLS = REGISTERED_TOOLS
