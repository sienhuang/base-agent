"""Model-visible Tools automatically loaded from this trusted Skill."""

from __future__ import annotations

from base_agent import tool

from .scripts.calculate import Number
from .scripts.calculate import calculate_product as _calculate_product
from .scripts.calculate import calculate_sum as _calculate_sum


@tool(permissions=frozenset({"skills:scripts:execute"}))
async def calculate_product(left: Number, right: Number) -> dict[str, Number]:
    """Multiply two numbers with the packaged Python calculation script."""
    return {"product": _calculate_product(left, right)}


@tool(permissions=frozenset({"skills:scripts:execute"}))
async def calculate_sum(left: Number, right: Number) -> dict[str, Number]:
    """Add two numbers with the packaged Python calculation script."""
    return {"sum": _calculate_sum(left, right)}
