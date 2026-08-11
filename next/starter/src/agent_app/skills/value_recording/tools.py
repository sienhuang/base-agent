"""Governed Tool entrypoints owned by the value-recording Skill."""

from __future__ import annotations

from base_agent import tool

from .scripts.record import record_integer


@tool(permissions=frozenset({"values:record"}))
async def record_value(value: int) -> int:
    """Record one integer through the Skill's private script."""
    return record_integer(value)
