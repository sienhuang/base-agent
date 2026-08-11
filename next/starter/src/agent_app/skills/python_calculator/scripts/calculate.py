"""Reusable calculation logic plus an optional JSON command-line entrypoint."""

from __future__ import annotations

import json
import sys

type Number = int | float


def calculate_sum(left: Number, right: Number) -> Number:
    """Return the sum used by the Skill's governed Tool."""
    return left + right


def calculate_product(left: Number, right: Number) -> Number:
    """Return the product used by the Skill's governed Tool."""
    return left * right


def main() -> None:
    payload = json.load(sys.stdin)
    left = payload.get("left")
    right = payload.get("right")
    if (
        not isinstance(left, int | float)
        or isinstance(left, bool)
        or not isinstance(right, int | float)
        or isinstance(right, bool)
    ):
        raise ValueError("left and right must be numbers")
    json.dump({"sum": calculate_sum(left, right)}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
