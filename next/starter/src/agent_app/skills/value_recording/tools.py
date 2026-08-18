"""Governed Tool entrypoints owned by the value-recording Skill."""

from __future__ import annotations

import json

from base_agent import ToolContext, tool

from .scripts.record import record_integer


@tool(permissions=frozenset({"values:record"}))
async def record_value(value: int, context: ToolContext) -> int:
    """Record one integer through the Skill's private script."""
    normalized = record_integer(value)
    if context.artifacts is None:
        raise RuntimeError("ArtifactManager is unavailable")
    await context.artifacts.create(
        name="recorded_value",
        media_type="application/json",
        content=json.dumps(
            {"value": normalized},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ),
        metadata={"value": normalized},
    )
    return normalized
