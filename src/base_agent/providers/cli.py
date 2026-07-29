"""Local Codex and Claude command-line adapters for the ModelProvider protocol."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from base_agent.models import (
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolChoice,
)
from base_agent.providers.errors import (
    CLIExecutableNotFoundError,
    CLIOutputLimitError,
    CLIProcessError,
    CLIProcessTimeoutError,
    InvalidProviderResponseError,
    UnsupportedAttachmentError,
    UnsupportedMemoryError,
)

_READ_CHUNK_SIZE = 64 * 1024
_PROCESS_STOP_TIMEOUT_SECONDS = 2.0
_DEFAULT_TIMEOUT_SECONDS = 300.0
_DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "content": {"type": ["string", "null"]},
        "tool_calls": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "arguments_json": {"type": "string"},
                },
                "required": ["name", "arguments_json"],
            },
        },
    },
    "required": ["content", "tool_calls"],
}


@dataclass(frozen=True, slots=True)
class CLIProcessOutput:
    """Bounded stdout/stderr captured from one local CLI process."""

    returncode: int
    stdout: str
    stderr: str


class CLIProcessRunner(Protocol):
    """Injectable subprocess boundary used by CLI providers and deterministic tests."""

    def __call__(
        self,
        command: tuple[str, ...],
        input_text: str,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        environment: Mapping[str, str] | None,
    ) -> Awaitable[CLIProcessOutput]: ...


class _CLIAdapter(Protocol):
    @property
    def name(self) -> str: ...

    def command(
        self,
        *,
        executable: str,
        model: str | None,
        schema_path: Path,
    ) -> tuple[str, ...]: ...

    def parse(self, stdout: str) -> tuple[dict[str, Any], TokenUsage, dict[str, Any]]: ...


class CLIModelProvider:
    """Execute a trusted local model CLI without invoking a shell."""

    def __init__(
        self,
        *,
        adapter: _CLIAdapter,
        executable: str,
        model: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        environment: Mapping[str, str] | None = None,
        runner: CLIProcessRunner | None = None,
    ) -> None:
        if not executable.strip():
            raise ValueError("CLI executable must not be blank")
        if model is not None and not model.strip():
            raise ValueError("CLI model must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("CLI timeout_seconds must be greater than zero")
        if max_output_bytes < 1:
            raise ValueError("CLI max_output_bytes must be greater than zero")
        self._adapter = adapter
        self._executable = executable
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes
        self._environment = dict(environment) if environment is not None else None
        self._runner = runner or run_cli_process

    @property
    def name(self) -> str:
        return self._adapter.name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.attachments:
            raise UnsupportedAttachmentError(
                f"{type(self).__name__} does not map attachments; process them through Tools"
            )
        if request.memories:
            raise UnsupportedMemoryError(
                f"{type(self).__name__} does not map memories; retrieve them through Tools"
            )
        prompt = _request_prompt(request)
        with tempfile.TemporaryDirectory(prefix="base-agent-cli-provider-") as temp_directory:
            working_directory = Path(temp_directory)
            schema_path = working_directory / "response-schema.json"
            schema_path.write_text(
                json.dumps(_RESPONSE_SCHEMA, ensure_ascii=False),
                encoding="utf-8",
            )
            command = self._adapter.command(
                executable=self._executable,
                model=request.model or self._model,
                schema_path=schema_path,
            )
            process = await self._runner(
                command,
                prompt,
                working_directory,
                self._timeout_seconds,
                self._max_output_bytes,
                self._environment,
            )
        if process.returncode != 0:
            detail = process.stderr.strip() or process.stdout.strip() or "no CLI error output"
            raise CLIProcessError(
                f"{self.name} exited with status {process.returncode}: {detail}"
            )
        payload, usage, metadata = self._adapter.parse(process.stdout)
        return _model_response(payload, usage=usage, metadata=metadata, request=request)


class CodexCLIProvider(CLIModelProvider):
    """Use `codex exec` as an ephemeral structured ModelProvider."""

    def __init__(
        self,
        *,
        model: str | None = None,
        executable: str = "codex",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        environment: Mapping[str, str] | None = None,
        runner: CLIProcessRunner | None = None,
    ) -> None:
        super().__init__(
            adapter=_CodexAdapter(),
            executable=executable,
            model=model,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            environment=environment,
            runner=runner,
        )


class ClaudeCLIProvider(CLIModelProvider):
    """Use `claude --print` with built-in Tools disabled as a ModelProvider."""

    def __init__(
        self,
        *,
        model: str | None = None,
        executable: str = "claude",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_output_bytes: int = _DEFAULT_MAX_OUTPUT_BYTES,
        environment: Mapping[str, str] | None = None,
        runner: CLIProcessRunner | None = None,
    ) -> None:
        super().__init__(
            adapter=_ClaudeAdapter(),
            executable=executable,
            model=model,
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            environment=environment,
            runner=runner,
        )


@dataclass(frozen=True, slots=True)
class _CodexAdapter:
    name: str = "codex-cli"

    def command(
        self,
        *,
        executable: str,
        model: str | None,
        schema_path: Path,
    ) -> tuple[str, ...]:
        command = [
            executable,
            "exec",
            "--json",
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--config",
            "shell_environment_policy.inherit=none",
            "--output-schema",
            str(schema_path),
        ]
        if model is not None:
            command.extend(("--model", model))
        command.append("-")
        return tuple(command)

    def parse(self, stdout: str) -> tuple[dict[str, Any], TokenUsage, dict[str, Any]]:
        events = _json_lines(stdout, provider=self.name)
        final_text: str | None = None
        usage = TokenUsage()
        metadata: dict[str, Any] = {}
        for event in events:
            event_type = event.get("type")
            if event_type == "thread.started" and event.get("thread_id") is not None:
                metadata["thread_id"] = event["thread_id"]
            if event_type == "item.completed":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str):
                        final_text = text
            if event_type == "turn.completed":
                usage = _token_usage(event.get("usage"))
            if event_type in {"turn.failed", "error"}:
                raise CLIProcessError(_event_error(event, provider=self.name))
        if final_text is None:
            raise InvalidProviderResponseError(
                "codex-cli output contains no completed agent message"
            )
        return _json_object(final_text, provider=self.name), usage, metadata


@dataclass(frozen=True, slots=True)
class _ClaudeAdapter:
    name: str = "claude-cli"

    def command(
        self,
        *,
        executable: str,
        model: str | None,
        schema_path: Path,
    ) -> tuple[str, ...]:
        schema = schema_path.read_text(encoding="utf-8")
        command = [
            executable,
            "--print",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--disable-slash-commands",
            "--tools",
            "",
            "--json-schema",
            schema,
        ]
        if model is not None:
            command.extend(("--model", model))
        return tuple(command)

    def parse(self, stdout: str) -> tuple[dict[str, Any], TokenUsage, dict[str, Any]]:
        outer = _json_object(stdout, provider=self.name)
        if outer.get("is_error") is True or outer.get("subtype") == "error":
            raise CLIProcessError(_event_error(outer, provider=self.name))
        structured = outer.get("structured_output")
        if isinstance(structured, dict):
            payload = structured
        else:
            result = outer.get("result")
            if not isinstance(result, str):
                raise InvalidProviderResponseError(
                    "claude-cli output has no structured_output or string result"
                )
            payload = _json_object(result, provider=self.name)
        metadata = _compact_mapping(
            outer,
            keys=(
                "session_id",
                "subtype",
                "duration_ms",
                "duration_api_ms",
                "num_turns",
                "total_cost_usd",
            ),
        )
        return payload, _token_usage(outer.get("usage")), metadata


async def run_cli_process(
    command: tuple[str, ...],
    input_text: str,
    cwd: Path,
    timeout_seconds: float,
    max_output_bytes: int,
    environment: Mapping[str, str] | None,
) -> CLIProcessOutput:
    """Run one argv-only process with bounded output, timeout, and cooperative cleanup."""
    resolved_environment = None
    if environment is not None:
        resolved_environment = {**os.environ, **environment}
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=cwd,
            env=resolved_environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise CLIExecutableNotFoundError(
            f"CLI executable was not found: {command[0]}"
        ) from exc

    stdout_task = asyncio.create_task(
        _read_bounded(process.stdout, max_output_bytes, stream_name="stdout")
    )
    stderr_task = asyncio.create_task(
        _read_bounded(process.stderr, max_output_bytes, stream_name="stderr")
    )
    wait_task = asyncio.create_task(process.wait())
    tasks = (stdout_task, stderr_task, wait_task)
    try:
        async with asyncio.timeout(timeout_seconds):
            await _write_input(process, input_text)
            stdout, stderr, returncode = await asyncio.gather(*tasks)
    except TimeoutError as exc:
        raise CLIProcessTimeoutError(
            f"CLI process exceeded timeout of {timeout_seconds:g} seconds"
        ) from exc
    finally:
        if process.returncode is None:
            await _stop_process(process)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return CLIProcessOutput(
        returncode=returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def _write_input(process: asyncio.subprocess.Process, input_text: str) -> None:
    if process.stdin is None:
        raise RuntimeError("CLI process stdin pipe is unavailable")
    process.stdin.write(input_text.encode("utf-8"))
    try:
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()


async def _read_bounded(
    stream: asyncio.StreamReader | None,
    limit: int,
    *,
    stream_name: str,
) -> bytes:
    if stream is None:
        raise RuntimeError(f"CLI process {stream_name} pipe is unavailable")
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await stream.read(_READ_CHUNK_SIZE)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            raise CLIOutputLimitError(
                f"CLI {stream_name} exceeded {limit} bytes"
            )
        chunks.append(chunk)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    with suppress(ProcessLookupError):
        process.terminate()
    try:
        async with asyncio.timeout(_PROCESS_STOP_TIMEOUT_SECONDS):
            await process.wait()
            return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        process.kill()
    await process.wait()


def _request_prompt(request: ModelRequest) -> str:
    envelope = {
        "messages": [
            message.model_dump(mode="json", exclude_none=True)
            for message in request.messages
        ],
        "tools": [
            tool.model_dump(mode="json")
            for tool in request.tools
        ],
        "tool_choice": request.tool_choice.value,
    }
    return (
        "Act only as a model backend for another Agent runtime. "
        "Do not inspect the host filesystem, run commands, call network services, or use your "
        "own built-in tools. Read the request JSON below and return one JSON object matching the "
        "provided response schema. Put a final answer in `content`. When an application Tool is "
        "needed, put it in `tool_calls` with its exact declared name and encode its argument "
        "object as compact JSON in `arguments_json`; do not execute that Tool yourself. Respect "
        "`tool_choice`: `none` forbids tool calls and `required` requires at least one.\n\n"
        f"REQUEST_JSON:\n{json.dumps(envelope, ensure_ascii=False)}"
    )


def _model_response(
    payload: dict[str, Any],
    *,
    usage: TokenUsage,
    metadata: dict[str, Any],
    request: ModelRequest,
) -> ModelResponse:
    content_value = payload.get("content")
    if content_value is not None and not isinstance(content_value, str):
        raise InvalidProviderResponseError("CLI response content must be a string or null")
    raw_calls = payload.get("tool_calls")
    if not isinstance(raw_calls, list):
        raise InvalidProviderResponseError("CLI response tool_calls must be an array")
    if request.tool_choice is ToolChoice.NONE and raw_calls:
        raise InvalidProviderResponseError(
            "CLI returned tool calls when tool_choice='none'"
        )
    if request.tool_choice is ToolChoice.REQUIRED and not raw_calls:
        raise InvalidProviderResponseError(
            "CLI returned no tool calls when tool_choice='required'"
        )
    calls: list[ToolCall] = []
    allowed_tool_names = {definition.name for definition in request.tools}
    for index, raw_call in enumerate(raw_calls, start=1):
        if not isinstance(raw_call, dict):
            raise InvalidProviderResponseError("CLI tool call must be an object")
        name = raw_call.get("name")
        arguments_json = raw_call.get("arguments_json")
        if not isinstance(name, str) or not isinstance(arguments_json, str):
            raise InvalidProviderResponseError(
                "CLI tool call requires string name and arguments_json"
            )
        try:
            arguments = json.loads(arguments_json)
        except json.JSONDecodeError as exc:
            raise InvalidProviderResponseError(
                f"CLI tool call '{name}' contains invalid arguments_json"
            ) from exc
        if not isinstance(arguments, dict):
            raise InvalidProviderResponseError(
                f"CLI tool call '{name}' arguments_json must decode to an object"
            )
        if name not in allowed_tool_names:
            raise InvalidProviderResponseError(
                f"CLI returned undeclared tool call '{name}'"
            )
        calls.append(
            ToolCall(
                id=f"cli-call-{index}",
                name=name,
                arguments=arguments,
            )
        )
    return ModelResponse(
        content=content_value,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage=usage,
        provider_metadata=metadata,
    )


def _json_lines(stdout: str, *, provider: str) -> tuple[dict[str, Any], ...]:
    events = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InvalidProviderResponseError(
                f"{provider} emitted invalid JSONL on line {line_number}"
            ) from exc
        if not isinstance(value, dict):
            raise InvalidProviderResponseError(
                f"{provider} JSONL line {line_number} is not an object"
            )
        events.append(value)
    if not events:
        raise InvalidProviderResponseError(f"{provider} emitted no JSON events")
    return tuple(events)


def _json_object(value: str, *, provider: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise InvalidProviderResponseError(
            f"{provider} response is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise InvalidProviderResponseError(
            f"{provider} response must be a JSON object"
        )
    return parsed


def _token_usage(value: Any) -> TokenUsage:
    if not isinstance(value, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_non_negative_int(value.get("input_tokens")),
        output_tokens=_non_negative_int(value.get("output_tokens")),
    )


def _non_negative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _event_error(event: Mapping[str, Any], *, provider: str) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return f"{provider} failed: {message}"
    if isinstance(error, str) and error:
        return f"{provider} failed: {error}"
    result = event.get("result")
    if isinstance(result, str) and result:
        return f"{provider} failed: {result}"
    return f"{provider} reported an error"


def _compact_mapping(value: Mapping[str, Any], *, keys: Sequence[str]) -> dict[str, Any]:
    return {key: value[key] for key in keys if value.get(key) is not None}
