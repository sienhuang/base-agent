"""Minimal asynchronous CLI entry point."""

from __future__ import annotations

import argparse
import asyncio

from agent_app.agent import build_agent


async def run(prompt: str, *, use_skill: bool = True) -> int:
    agent = build_agent()
    result = await agent.run(
        prompt,
        skills=("text-analysis",) if use_skill else (),
    )
    if result.output:
        print(result.output)
    if result.error:
        print(f"error: {result.error}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the starter Agent.")
    parser.add_argument("prompt", nargs="+", help="Task for the Agent")
    parser.add_argument(
        "--no-skill",
        action="store_true",
        help="Run without selecting the example text-analysis Skill",
    )
    arguments = parser.parse_args()
    raise SystemExit(
        asyncio.run(run(" ".join(arguments.prompt), use_skill=not arguments.no_skill))
    )
