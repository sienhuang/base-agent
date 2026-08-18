from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from typing import Literal, cast

from base_agent import LocalArtifactStore
from base_agent.models import Event

from agent_app.app import run, run_plan_react
from agent_app.config import ProviderName, Settings, StructuredOutputMode
from agent_app.provider import build_plan_react_providers, build_provider

RunMode = Literal["agent", "plan-react"]


async def _run(
    prompt: str,
    *,
    settings: Settings,
    show_tool_results: bool,
    show_execution_trace: bool,
    stream: bool,
    flow: RunMode = "agent",
) -> int:
    try:
        provider = build_provider(settings)
        if flow == "plan-react":
            planner_provider, executor_provider = build_plan_react_providers(
                settings,
                default_provider=provider,
            )
        else:
            planner_provider = executor_provider = provider
    except (ImportError, ValueError) as exc:
        print(f"configuration error: {exc}")
        return 2

    streamed_text = False

    def observe_event(event: Event) -> None:
        nonlocal streamed_text
        if show_execution_trace:
            line = _execution_trace_line(
                event,
                include_run_id=(flow == "plan-react"),
            )
            if line is not None:
                print(line, file=sys.stderr, flush=True)
        if stream and event.kind == "message.delta":
            text = event.payload.get("delta")
            if isinstance(text, str) and text:
                print(text, end="", flush=True)
                streamed_text = True

    observer = observe_event if stream or show_execution_trace else None
    artifact_store = LocalArtifactStore(settings.artifact_root)
    try:
        if flow == "plan-react":
            flow_result = await run_plan_react(
                prompt,
                model=provider,
                planner_provider=planner_provider,
                executor_provider=executor_provider,
                planner_model=settings.plan_react_planner_model,
                executor_model=settings.plan_react_executor_model,
                stream=stream,
                on_event=observer,
                artifact_store=artifact_store,
            )
            if flow_result.output:
                print(flow_result.output)
            if show_tool_results:
                for child in flow_result.child_runs:
                    for item in child.run.tool_results:
                        line = (
                            f"{child.agent_run_id}/{item.call.id}: "
                            f"status={item.status.value}, data={item.data!r}"
                        )
                        if item.error is not None:
                            line += f", error={item.error!r}"
                        print(line)
            if flow_result.error is not None:
                print(f"error: {flow_result.error}")
                return 1
            return 0

        result = await run(
            prompt,
            model=provider,
            stream=stream,
            on_event=observer,
            artifact_store=artifact_store,
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


def _execution_trace_line(
    event: Event,
    *,
    include_run_id: bool = False,
) -> str | None:
    if event.payload.get("event_scope") == "child_run":
        child_payload = dict(event.payload)
        child_payload.pop("event_scope", None)
        child_kind = event.kind.removeprefix("child.")
        return _execution_trace_line(
            Event(
                sequence=event.payload["child_sequence"],
                kind=child_kind,
                run_id=event.payload["agent_run_id"],
                turn=event.turn,
                payload=child_payload,
            ),
            include_run_id=True,
        )
    trace_id = f"{event.run_id[:8]}:{event.sequence}" if include_run_id else str(event.sequence)
    prefix = f"[trace {trace_id}] {event.kind}"
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
        return f"{prefix} turn={event.turn} tools={event.payload['tool_count']}"
    if event.kind == "model.responded":
        return (
            f"{prefix} turn={event.turn} tool_calls={event.payload['tool_call_count']}"
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
    if event.kind == "artifact.created":
        return (
            f"{prefix} artifact={event.payload['name']} "
            f"id={event.payload['artifact_id']}"
        )
    if event.kind == "artifact.granted":
        return (
            f"{prefix} artifact={event.payload['name']} "
            f"id={event.payload['artifact_id']} "
            f"producer={event.payload['producer_run_id']}"
        )
    if event.kind == "run.succeeded":
        return f"{prefix} turn={event.turn}"
    if event.kind == "run.failed":
        return f"{prefix} error={event.payload['error']!r}"
    if event.kind == "flow.started":
        return (
            f"{prefix} flow={event.payload['flow_id']} "
            f"plan={event.payload['plan_id']}"
        )
    if event.kind == "planner.started":
        return (
            f"{prefix} phase={event.payload['phase']} "
            f"run={event.payload['agent_run_id']}"
        )
    if event.kind == "planner.completed":
        return (
            f"{prefix} phase={event.payload['phase']} "
            f"action={event.payload['action']} run={event.payload['agent_run_id']}"
        )
    if event.kind == "plan.created":
        return (
            f"{prefix} plan={event.payload['plan_id']} "
            f"revision={event.payload['plan_revision']}"
        )
    if event.kind == "plan.reviewed":
        return (
            f"{prefix} revision={event.payload['plan_revision']} "
            f"step={event.payload['step_id']} action={event.payload['action']}"
        )
    if event.kind == "plan.revised":
        return (
            f"{prefix} plan={event.payload['plan_id']} "
            f"revision={event.payload['source_plan_revision']}"
            f"->{event.payload['target_plan_revision']}"
        )
    if event.kind == "step.started":
        return (
            f"{prefix} revision={event.payload['plan_revision']} "
            f"step={event.payload['step_id']} attempt={event.payload['attempt']} "
            f"run={event.payload['agent_run_id']}"
        )
    if event.kind in {"step.completed", "step.failed"}:
        return (
            f"{prefix} revision={event.payload['plan_revision']} "
            f"step={event.payload['step_id']} success={event.payload.get('success')} "
            f"run={event.payload['agent_run_id']}"
        )
    if event.kind in {"plan.completed", "plan.failed", "plan.cancelled"}:
        return (
            f"{prefix} plan={event.payload['plan_id']} "
            f"revision={event.payload['plan_revision']}"
        )
    if event.kind in {"flow.succeeded", "flow.failed", "flow.cancelled"}:
        return f"{prefix} plan={event.payload['plan_id']}"
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Starter Agent.")
    parser.add_argument(
        "--flow",
        choices=("agent", "plan-react"),
        default="agent",
        help="Run the ordinary Agent or the fixed Planner-ReAct Flow",
    )
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
        "--planner-model",
        help=(
            "Planner model for plan-react; defaults to "
            "PLAN_REACT_PLANNER_MODEL, then --model/AGENT_MODEL"
        ),
    )
    parser.add_argument(
        "--executor-model",
        help=(
            "Executor model for plan-react; defaults to "
            "PLAN_REACT_EXECUTOR_MODEL, then --model/AGENT_MODEL"
        ),
    )
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible API base URL; defaults to OPENAI_BASE_URL",
    )
    parser.add_argument(
        "--structured-output-mode",
        choices=("json-schema", "json-object", "prompt"),
        help=(
            "OpenAI-compatible structured output transport; defaults to "
            "AGENT_STRUCTURED_OUTPUT_MODE or prompt"
        ),
    )
    parser.add_argument(
        "--cli-executable",
        help="Override AGENT_CLI_EXECUTABLE for codex-cli or claude-cli",
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
    parser.add_argument("prompt", nargs="+", help="User request")
    arguments = parser.parse_args()
    provider_override = (
        cast(ProviderName, arguments.provider) if arguments.provider is not None else None
    )
    try:
        settings = Settings.from_env(provider=provider_override)
    except ValueError as exc:
        parser.error(str(exc))
    if provider_override in {"codex-cli", "claude-cli"} and arguments.model is None:
        settings = replace(
            settings,
            model=None,
            plan_react_planner_model=None,
            plan_react_executor_model=None,
        )
    if arguments.model is not None:
        if not arguments.model.strip():
            parser.error("--model must not be blank")
        settings = replace(settings, model=arguments.model.strip())
    if arguments.planner_model is not None:
        if not arguments.planner_model.strip():
            parser.error("--planner-model must not be blank")
        settings = replace(
            settings,
            plan_react_planner_model=arguments.planner_model.strip(),
        )
    if arguments.executor_model is not None:
        if not arguments.executor_model.strip():
            parser.error("--executor-model must not be blank")
        settings = replace(
            settings,
            plan_react_executor_model=arguments.executor_model.strip(),
        )
    if arguments.base_url is not None:
        if not arguments.base_url.strip():
            parser.error("--base-url must not be blank")
        settings = replace(settings, base_url=arguments.base_url.strip())
    if arguments.structured_output_mode is not None:
        settings = replace(
            settings,
            structured_output_mode=cast(
                StructuredOutputMode,
                arguments.structured_output_mode,
            ),
        )
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
                show_tool_results=arguments.show_tool_results,
                show_execution_trace=arguments.show_execution_trace,
                stream=arguments.stream,
                flow=cast(RunMode, arguments.flow),
            )
        )
    )
