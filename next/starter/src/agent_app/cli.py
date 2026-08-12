from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from typing import cast

from base_agent.governance import AllowAllConfirmations
from base_agent.models import Event

from agent_app.app import run
from agent_app.config import ProviderName, Settings
from agent_app.provider import build_provider


async def _run(
    prompt: str,
    *,
    settings: Settings,
    permissions: frozenset[str],
    max_model_turns: int,
    max_tool_calls_per_turn: int,
    show_tool_results: bool,
    show_execution_trace: bool,
    approve_side_effects: bool,
    enable_browser_tool: bool,
    enable_recording_skill: bool,
    enable_python_script_skill: bool,
    stream: bool,
) -> int:
    try:
        provider = build_provider(settings)
    except (ImportError, ValueError) as exc:
        print(f"configuration error: {exc}")
        return 2

    streamed_text = False

    def observe_event(event: Event) -> None:
        nonlocal streamed_text
        if show_execution_trace:
            line = _execution_trace_line(event)
            if line is not None:
                print(line, file=sys.stderr, flush=True)
        if stream and event.kind == "message.delta":
            text = event.payload.get("delta")
            if isinstance(text, str) and text:
                print(text, end="", flush=True)
                streamed_text = True

    try:
        result = await run(
            prompt,
            model=provider,
            permissions=permissions,
            max_model_turns=max_model_turns,
            max_tool_calls_per_turn=max_tool_calls_per_turn,
            confirmation_policy=(AllowAllConfirmations() if approve_side_effects else None),
            enable_browser_tool=enable_browser_tool,
            enable_recording_skill=enable_recording_skill,
            enable_python_script_skill=enable_python_script_skill,
            stream=stream,
            on_event=(observe_event if stream or show_execution_trace else None),
        )
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return 2
    if streamed_text:
        print()
    if result.text and not streamed_text:
        print(result.output)
    if show_tool_results:
        for item in result.tool_results:
            line = f"{item.call.id}: status={item.status.value}, data={item.data!r}"
            if item.error is not None:
                line += f", error={item.error!r}"
            print(line)
    if result.error is not None:
        print(f"error: {result.error}")
        return 1
    return 0


def _execution_trace_line(event: Event) -> str | None:
    prefix = f"[trace {event.sequence}] {event.kind}"
    if event.kind == "run.started":
        return f"{prefix} agent={event.payload['agent_id']}"
    if event.kind in {"skill.selected", "skill.loaded", "skill.file.loaded"}:
        file_detail = f" file={event.payload['file']}" if event.kind != "skill.selected" else ""
        return (
            f"{prefix} skill={event.payload['skill_name']} "
            f"version={event.payload['skill_version']}"
            f"{file_detail}"
        )
    if event.kind == "run.ready":
        return f"{prefix} tools={event.payload['tool_count']}"
    if event.kind == "model.requested":
        return f"{prefix} turn={event.payload['turn']} tools={event.payload['tool_count']}"
    if event.kind == "model.responded":
        return (
            f"{prefix} turn={event.payload['turn']} tool_calls={event.payload['tool_call_count']}"
        )
    if event.kind == "skill.resource.used":
        return (
            f"{prefix} skill={event.payload['skill_name']} "
            f"type={event.payload['resource_type']} "
            f"resource={event.payload['resource']} "
            f"tool={event.payload['tool_name']} "
            f"call={event.payload['tool_call_id']}"
        )
    if event.kind == "tool.started":
        return f"{prefix} tool={event.payload['tool_name']} call={event.payload['tool_call_id']}"
    if event.kind == "tool.completed":
        return (
            f"{prefix} tool={event.payload['tool_name']} "
            f"call={event.payload['tool_call_id']} "
            f"status={event.payload['status']}"
        )
    if event.kind == "run.succeeded":
        return f"{prefix} turn={event.payload['turn']}"
    if event.kind == "run.failed":
        return f"{prefix} error={event.payload['error']!r}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Starter Agent.")
    parser.add_argument(
        "--provider",
        choices=(
            "offline",
            "openai-compatible",
            "codex-cli",
            "claude-cli",
        ),
        help="Override AGENT_PROVIDER (default: offline)",
    )
    parser.add_argument(
        "--model",
        help="OpenAI-compatible model name; defaults to OPENAI_MODEL",
    )
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible API base URL; defaults to OPENAI_BASE_URL",
    )
    parser.add_argument(
        "--cli-executable",
        help="Override AGENT_CLI_EXECUTABLE for codex-cli or claude-cli",
    )
    parser.add_argument(
        "--max-model-turns",
        type=int,
        default=10,
        help="Maximum model calls including finalization (default: 10)",
    )
    parser.add_argument(
        "--max-tool-calls-per-turn",
        type=int,
        default=3,
        help="ToolCalls executed from one model response (default: 3)",
    )
    parser.add_argument(
        "--show-tool-results",
        action="store_true",
        help="Print every governed ToolResult after the final answer",
    )
    parser.add_argument(
        "--show-execution-trace",
        action="store_true",
        help="Print the Run, Skill, model and Tool execution chain",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream model text deltas while the Agent runs",
    )
    parser.add_argument(
        "--approve-side-effects",
        action="store_true",
        help="Approve confirmation-required ToolCalls for this demo run",
    )
    parser.add_argument(
        "--enable-recording-skill",
        action="store_true",
        help=(
            "Enable the value-recording Skill playbook and its contributed record_value capability"
        ),
    )
    parser.add_argument(
        "--enable-python-script-skill",
        action="store_true",
        help="Enable the Python calculator Skill and direct script execution",
    )
    parser.add_argument(
        "--enable-browser-tool",
        action="store_true",
        help="Enable the opt-in browser Tool and its readiness preflight",
    )
    parser.add_argument(
        "--grant-permission",
        action="append",
        default=[],
        metavar="PERMISSION",
        help="Grant one Agent permission; may be repeated",
    )
    parser.add_argument("prompt", nargs="+", help="User request")
    arguments = parser.parse_args()
    if arguments.max_model_turns < 2:
        parser.error("--max-model-turns must be at least 2")
    if arguments.max_tool_calls_per_turn < 1:
        parser.error("--max-tool-calls-per-turn must be at least 1")
    permissions = frozenset(permission.strip() for permission in arguments.grant_permission)
    if "" in permissions:
        parser.error("--grant-permission must not be blank")
    provider_override = (
        cast(ProviderName, arguments.provider) if arguments.provider is not None else None
    )
    try:
        settings = Settings.from_env(provider=provider_override)
    except ValueError as exc:
        parser.error(str(exc))
    if provider_override in {"codex-cli", "claude-cli"} and arguments.model is None:
        settings = replace(settings, model=None)
    if arguments.model is not None:
        if not arguments.model.strip():
            parser.error("--model must not be blank")
        settings = replace(settings, model=arguments.model.strip())
    if arguments.base_url is not None:
        if not arguments.base_url.strip():
            parser.error("--base-url must not be blank")
        settings = replace(settings, base_url=arguments.base_url.strip())
    if arguments.cli_executable is not None:
        if not arguments.cli_executable.strip():
            parser.error("--cli-executable must not be blank")
        settings = replace(
            settings,
            cli_executable=arguments.cli_executable.strip(),
        )
    raise SystemExit(
        asyncio.run(
            _run(
                " ".join(arguments.prompt),
                settings=settings,
                permissions=permissions,
                max_model_turns=arguments.max_model_turns,
                max_tool_calls_per_turn=(arguments.max_tool_calls_per_turn),
                show_tool_results=arguments.show_tool_results,
                show_execution_trace=arguments.show_execution_trace,
                approve_side_effects=arguments.approve_side_effects,
                enable_browser_tool=arguments.enable_browser_tool,
                enable_recording_skill=(arguments.enable_recording_skill),
                enable_python_script_skill=(arguments.enable_python_script_skill),
                stream=arguments.stream,
            )
        )
    )
