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


async def run_chat(*, use_skill: bool = True) -> int:
    """Run an interactive Conversation whose every user Turn is a normal Run."""
    agent = build_agent()
    conversation = await agent.create_conversation()
    print(f"conversation_id={conversation.id}")
    print("Enter /exit to stop.")
    while True:
        prompt = (await asyncio.to_thread(input, "you> ")).strip()
        if prompt in {"/exit", "/quit"}:
            return 0
        if not prompt:
            continue
        result = await agent.run(
            prompt,
            conversation_id=conversation.id,
            skills=("text-analysis",) if use_skill else (),
        )
        if result.output:
            print(f"agent> {result.output}")
        if result.error:
            print(f"error: {result.error}")
            return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the starter Agent.")
    parser.add_argument("prompt", nargs="*", help="Task for the Agent")
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Start an interactive multi-Run Conversation",
    )
    parser.add_argument(
        "--no-skill",
        action="store_true",
        help="Run without selecting the example text-analysis Skill",
    )
    arguments = parser.parse_args()
    if arguments.chat:
        raise SystemExit(asyncio.run(run_chat(use_skill=not arguments.no_skill)))
    if not arguments.prompt:
        parser.error("prompt is required unless --chat is used")
    raise SystemExit(asyncio.run(run(" ".join(arguments.prompt), use_skill=not arguments.no_skill)))
