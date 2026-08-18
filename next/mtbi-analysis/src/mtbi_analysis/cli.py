"""Command-line entry point for the MTBI application."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

from mtbi_analysis.app import build_application
from mtbi_analysis.config import ProviderName, Settings, StructuredOutputMode
from mtbi_analysis.tools import MTBIEnvironmentError


async def _run(prompt: str, settings: Settings) -> int:
    try:
        application = build_application(settings)
        result = await application.run(prompt)
    except (ImportError, MTBIEnvironmentError, OSError, ValueError) as exc:
        print(f"configuration error: {exc}")
        return 2

    if result.dataset is not None:
        print(f"SQL: {result.dataset.sql_path}")
        print(f"CSV: {result.dataset.csv_path}")
    elif result.query is not None and not result.query.success:
        print(f"Query failed: {result.query.error}")
    if result.dataset is not None:
        print(
            f"Dataset verified: rows={result.dataset.row_count}, "
            f"columns={len(result.dataset.columns)}"
        )
    if result.analysis is not None:
        print(result.analysis.summary)
    if result.report is not None:
        print(f"Report: {result.report.report_path}")
    if result.error is not None:
        for stage in result.flow.stage_attempts or result.flow.stages:
            for tool_result in stage.run.tool_results:
                diagnostic = (
                    f"tool: stage={stage.name}, attempt={stage.attempt}, "
                    f"name={tool_result.call.name}, "
                    f"status={tool_result.status.value}"
                )
                if tool_result.error is not None:
                    diagnostic += f", error={tool_result.error}"
                print(diagnostic)
        print(f"error: {result.error}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the independent base-agent MTBI analysis application."
    )
    parser.add_argument("prompt", nargs="+", help="MTBI question to query and analyze")
    parser.add_argument(
        "--provider",
        choices=("codex-cli", "openai-compatible"),
        help="Override AGENT_PROVIDER",
    )
    parser.add_argument("--model", help="Override AGENT_MODEL or OPENAI_MODEL")
    parser.add_argument("--base-url", help="Override OPENAI_BASE_URL")
    parser.add_argument("--cli-executable", help="Override AGENT_CLI_EXECUTABLE")
    parser.add_argument("--mtbi-cli-bin", help="Override MTBI_CLI_BIN")
    parser.add_argument("--workspace", help="Override MTBI_WORKSPACE")
    parser.add_argument(
        "--structured-output-mode",
        choices=("json-schema", "json-object", "prompt"),
        help="Override MTBI_STRUCTURED_OUTPUT_MODE",
    )
    arguments = parser.parse_args()

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        parser.error(str(exc))
    if arguments.provider is not None:
        settings = replace(
            settings,
            provider=cast(ProviderName, arguments.provider),
            model=(
                None
                if arguments.provider == "codex-cli" and arguments.model is None
                else settings.model
            ),
        )
    if arguments.model is not None:
        settings = replace(settings, model=_non_blank(parser, "--model", arguments.model))
    if arguments.base_url is not None:
        settings = replace(
            settings,
            base_url=_non_blank(parser, "--base-url", arguments.base_url),
        )
    if arguments.cli_executable is not None:
        settings = replace(
            settings,
            cli_executable=_non_blank(
                parser,
                "--cli-executable",
                arguments.cli_executable,
            ),
        )
    if arguments.mtbi_cli_bin is not None:
        settings = replace(
            settings,
            mtbi_cli_bin=_non_blank(
                parser,
                "--mtbi-cli-bin",
                arguments.mtbi_cli_bin,
            ),
        )
    if arguments.workspace is not None:
        workspace = Path(
            _non_blank(parser, "--workspace", arguments.workspace)
        ).expanduser().resolve()
        settings = replace(settings, workspace=workspace)
    if arguments.structured_output_mode is not None:
        settings = replace(
            settings,
            structured_output_mode=cast(
                StructuredOutputMode,
                arguments.structured_output_mode,
            ),
        )
    raise SystemExit(asyncio.run(_run(" ".join(arguments.prompt), settings)))


def _non_blank(parser: argparse.ArgumentParser, option: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        parser.error(f"{option} must not be blank")
    return normalized
