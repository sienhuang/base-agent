"""Minimal asynchronous CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from typing import cast
from uuid import UUID

from base_agent import Agent, AgentResult, EventType, RuntimeEvent

from agent_app.agent import build_agent
from agent_app.config import ProviderName, Settings


def _print_tool_failure(event: RuntimeEvent) -> None:
    if event.type is not EventType.TOOL_FAILED:
        return
    payload = event.data.get("result")
    if not isinstance(payload, dict):
        return
    print(
        f"[tool.failed] run_id={event.run_id} "
        f"tool={payload.get('tool_name', 'unknown')} "
        f"call_id={event.data.get('call_id', 'unknown')} "
        f"error_code={payload.get('error_code', 'unknown')}",
        flush=True,
    )
    message = payload.get("message")
    if isinstance(message, str) and message:
        print(f"message: {message}", flush=True)


async def _execute(
    agent: Agent,
    prompt: str,
    *,
    skills: tuple[str, ...],
    planning: bool,
    conversation_id: UUID | None = None,
) -> AgentResult:
    result = await agent.run(
        prompt,
        conversation_id=conversation_id,
        skills=skills,
        planning=planning,
    )
    run_id = UUID(str(result.metadata["run_id"]))
    for event in await agent.events(run_id):
        _print_tool_failure(event)
    return result


async def run(
    prompt: str,
    *,
    use_skill: bool = True,
    planning: bool = False,
    react: bool = False,
    settings: Settings | None = None,
) -> int:
    agent = build_agent(settings, react=react)
    result = await _execute(
        agent,
        prompt,
        skills=("text-analysis",) if use_skill else (),
        planning=planning,
    )
    if result.output:
        print(result.output)
    if result.error:
        print(f"error: {result.error}")
        return 1
    return 0


async def run_chat(
    *,
    use_skill: bool = True,
    planning: bool = False,
    react: bool = False,
    settings: Settings | None = None,
) -> int:
    """Run an interactive Conversation whose every user Turn is a normal Run."""
    agent = build_agent(settings, react=react)
    conversation = await agent.create_conversation()
    print(f"conversation_id={conversation.id}")
    print("Enter /exit to stop.")
    while True:
        prompt = (await asyncio.to_thread(input, "you> ")).strip()
        if prompt in {"/exit", "/quit"}:
            return 0
        if not prompt:
            continue
        result = await _execute(
            agent,
            prompt,
            conversation_id=conversation.id,
            skills=("text-analysis",) if use_skill else (),
            planning=planning,
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
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument(
        "--plan",
        action="store_true",
        help="Generate and execute a Plan inside the same Run",
    )
    execution.add_argument(
        "--react",
        action="store_true",
        help="Run the whole task with observable ReAct iterations",
    )
    parser.add_argument(
        "--no-skill",
        action="store_true",
        help="Run without selecting the example text-analysis Skill",
    )
    parser.add_argument(
        "--provider",
        choices=("offline", "openai", "codex-cli", "claude-cli"),
        help="Override AGENT_PROVIDER for this invocation",
    )
    parser.add_argument(
        "--model",
        help="Override AGENT_MODEL; omit to use the local CLI default model",
    )
    parser.add_argument(
        "--cli-executable",
        help="Override the codex or claude executable path",
    )
    parser.add_argument(
        "--coding",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable the isolated CodingBundle",
    )
    parser.add_argument(
        "--sandbox-image",
        help="Docker image for --coding, for example python:3.12",
    )
    parser.add_argument(
        "--web-search",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable Brave Web Search; the API key comes from BRAVE_SEARCH_API_KEY",
    )
    parser.add_argument(
        "--mtbi",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable the mtbi-cli metadata and bounded OneSQL DataSource",
    )
    parser.add_argument(
        "--mtbi-cli-executable",
        help="Override AGENT_MTBI_CLI_EXECUTABLE (default: mtbi-cli)",
    )
    parser.add_argument(
        "--mtbi-engine",
        choices=("PRESTO", "SPARK", "DORIS"),
        help="OneSQL execution engine",
    )
    parser.add_argument(
        "--mtbi-region",
        help="Override the mtbi-cli region",
    )
    arguments = parser.parse_args()
    provider_override = (
        cast(ProviderName, arguments.provider)
        if arguments.provider is not None
        else None
    )
    settings = Settings.from_env(provider=provider_override)
    if (
        provider_override in {"codex-cli", "claude-cli"}
        and arguments.model is None
    ):
        settings = replace(settings, model=None)
    if arguments.model is not None:
        if not arguments.model.strip():
            parser.error("--model must not be blank")
        settings = replace(settings, model=arguments.model.strip())
    if arguments.cli_executable is not None:
        if not arguments.cli_executable.strip():
            parser.error("--cli-executable must not be blank")
        settings = replace(
            settings,
            cli_executable=arguments.cli_executable.strip(),
        )
    if arguments.coding is not None:
        settings = replace(settings, enable_coding=arguments.coding)
    if arguments.sandbox_image is not None:
        if not arguments.sandbox_image.strip():
            parser.error("--sandbox-image must not be blank")
        settings = replace(
            settings,
            enable_coding=True,
            sandbox_image=arguments.sandbox_image.strip(),
        )
    if arguments.web_search is not None:
        settings = replace(settings, enable_web_search=arguments.web_search)
    if arguments.mtbi is not None:
        settings = replace(settings, enable_mtbi=arguments.mtbi)
    if arguments.mtbi_cli_executable is not None:
        if not arguments.mtbi_cli_executable.strip():
            parser.error("--mtbi-cli-executable must not be blank")
        settings = replace(
            settings,
            mtbi_cli_executable=arguments.mtbi_cli_executable.strip(),
        )
    if arguments.mtbi_engine is not None:
        settings = replace(settings, mtbi_engine=arguments.mtbi_engine)
    if arguments.mtbi_region is not None:
        if not arguments.mtbi_region.strip():
            parser.error("--mtbi-region must not be blank")
        settings = replace(settings, mtbi_region=arguments.mtbi_region.strip())
    if settings.enable_coding and settings.sandbox_image is None:
        parser.error("--coding requires --sandbox-image or AGENT_SANDBOX_IMAGE")
    if settings.enable_web_search and settings.brave_search_api_key is None:
        parser.error("--web-search requires BRAVE_SEARCH_API_KEY")
    if arguments.chat:
        raise SystemExit(
            asyncio.run(
                run_chat(
                    use_skill=not arguments.no_skill,
                    planning=arguments.plan,
                    react=arguments.react,
                    settings=settings,
                )
            )
        )
    if not arguments.prompt:
        parser.error("prompt is required unless --chat is used")
    raise SystemExit(
        asyncio.run(
            run(
                " ".join(arguments.prompt),
                use_skill=not arguments.no_skill,
                planning=arguments.plan,
                react=arguments.react,
                settings=settings,
            )
        )
    )
