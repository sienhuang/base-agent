import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from base_agent.models import (
    FinishReason,
    Message,
    ModelRequest,
    ToolChoice,
    ToolSpec,
)
from base_agent.providers import (
    ClaudeCLIProvider,
    CLIExecutableNotFoundError,
    CLIOutputLimitError,
    CLIProcessError,
    CLIProcessOutput,
    CLIProcessTimeoutError,
    CodexCLIProvider,
    InvalidProviderResponseError,
    ModelProvider,
    ProviderCapabilities,
    ProviderInfo,
    run_cli_process,
)


class FakeRunner:
    def __init__(self, output: CLIProcessOutput) -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        command: tuple[str, ...],
        input_text: str,
        cwd: Path,
        timeout_seconds: float,
        max_output_bytes: int,
        environment: Mapping[str, str] | None,
    ) -> CLIProcessOutput:
        schema_path = cwd / "response-schema.json"
        self.calls.append(
            {
                "command": command,
                "input_text": input_text,
                "cwd_name": cwd.name,
                "schema": json.loads(schema_path.read_text(encoding="utf-8")),
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
                "environment": environment,
            }
        )
        return self.output


def _request(
    *,
    model: str = "request-model",
    tool_choice: ToolChoice = ToolChoice.REQUIRED,
) -> ModelRequest:
    return ModelRequest(
        model=model,
        system_prompt="Be concise.",
        messages=(
            Message.user(
                "Weather in Shanghai?",
                message_id="runtime-message-1",
            ),
        ),
        tools=(
            ToolSpec(
                name="weather",
                description="Read weather",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            ),
        ),
        tool_choice=tool_choice,
    )


async def test_codex_cli_maps_jsonl_tool_calls_usage_and_safe_command() -> None:
    response_payload = {
        "content": None,
        "tool_calls": [{"name": "weather", "arguments_json": '{"city":"上海"}'}],
    }
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(
                            response_payload,
                            ensure_ascii=False,
                        ),
                    },
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 21,
                        "cached_input_tokens": 7,
                        "output_tokens": 5,
                    },
                }
            ),
        ]
    )
    runner = FakeRunner(CLIProcessOutput(returncode=0, stdout=stdout, stderr=""))
    provider = CodexCLIProvider(
        executable="/trusted/codex",
        runner=runner,
        timeout_seconds=12,
        max_output_bytes=123_456,
    )

    response = await provider.complete(_request())
    call = runner.calls[0]

    assert isinstance(provider, ModelProvider)
    assert provider.info == ProviderInfo(
        name="codex-cli",
        default_model="codex-cli-default",
        capabilities=ProviderCapabilities(
            tools=True,
            structured_output=True,
            streaming=False,
        ),
    )
    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].id.startswith("cli-call-")
    assert response.tool_calls[0].arguments == {"city": "上海"}
    assert response.usage.total_tokens == 26
    assert response.usage.cached_input_tokens == 7
    assert response.provider_data["thread_id"] == "thread-123"
    command = call["command"]
    assert command[:13] == (
        "/trusted/codex",
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
    )
    assert Path(command[13]).name == "response-schema.json"
    assert Path(command[13]).parent.name == call["cwd_name"]
    assert command[-3:] == ("--model", "request-model", "-")
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert call["schema"]["additionalProperties"] is False
    assert '"system_prompt": "Be concise."' in call["input_text"]
    assert '"tool_choice": "required"' in call["input_text"]
    assert '"name": "weather"' in call["input_text"]
    envelope = json.loads(call["input_text"].split("REQUEST_JSON:\n", 1)[1])
    assert set(envelope) == {
        "system_prompt",
        "messages",
        "tools",
        "tool_choice",
        "response_schema",
    }
    assert envelope["messages"] == [{"role": "user", "content": "Weather in Shanghai?"}]
    assert "message_id" not in call["input_text"]
    assert call["timeout_seconds"] == 12
    assert call["max_output_bytes"] == 123_456


async def test_cli_provider_generates_unique_tool_call_ids_across_model_turns() -> None:
    response_payload = {
        "content": None,
        "tool_calls": [{"name": "weather", "arguments_json": '{"city":"上海"}'}],
    }
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(response_payload, ensure_ascii=False),
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    provider = CodexCLIProvider(
        runner=FakeRunner(CLIProcessOutput(returncode=0, stdout=stdout, stderr=""))
    )

    first_response = await provider.complete(_request())
    second_response = await provider.complete(_request())

    first_id = first_response.tool_calls[0].id
    second_id = second_response.tool_calls[0].id
    assert first_id.startswith("cli-call-")
    assert second_id.startswith("cli-call-")
    assert first_id != second_id


async def test_cli_provider_can_use_the_local_cli_default_model() -> None:
    payload = {"content": "done", "tool_calls": []}
    stdout = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": json.dumps(payload),
                    },
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    runner = FakeRunner(CLIProcessOutput(returncode=0, stdout=stdout, stderr=""))
    provider = CodexCLIProvider(runner=runner)

    response = await provider.complete(
        ModelRequest(
            model=provider.info.default_model or "",
            system_prompt="Answer.",
            messages=(Message.user("hello"),),
            tools=(),
            tool_choice=ToolChoice.AUTO,
        )
    )

    assert response.content == "done"
    assert "--model" not in runner.calls[0]["command"]


async def test_claude_cli_disables_tools_and_maps_structured_output() -> None:
    stdout = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "session-123",
            "duration_ms": 42,
            "total_cost_usd": 0.01,
            "structured_output": {
                "content": "Shanghai is sunny.",
                "tool_calls": [],
            },
            "usage": {"input_tokens": 8, "output_tokens": 4},
        }
    )
    runner = FakeRunner(CLIProcessOutput(returncode=0, stdout=stdout, stderr=""))
    provider = ClaudeCLIProvider(
        executable="/trusted/claude",
        runner=runner,
    )

    response = await provider.complete(
        ModelRequest(
            model="claude-model",
            system_prompt="Answer.",
            messages=(Message.user("Weather?"),),
            tools=(),
            tool_choice=ToolChoice.AUTO,
        )
    )
    command = runner.calls[0]["command"]

    assert provider.info.name == "claude-cli"
    assert response.content == "Shanghai is sunny."
    assert response.usage.total_tokens == 12
    assert response.provider_data["session_id"] == "session-123"
    assert command[:4] == (
        "/trusted/claude",
        "--print",
        "--output-format",
        "json",
    )
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command
    assert "--dangerously-skip-permissions" not in command


async def test_cli_provider_rejects_process_and_tool_choice_failures() -> None:
    failed_runner = FakeRunner(
        CLIProcessOutput(
            returncode=2,
            stdout="",
            stderr="authentication failed",
        )
    )
    provider = ClaudeCLIProvider(runner=failed_runner)
    with pytest.raises(CLIProcessError, match="authentication failed"):
        await provider.complete(
            ModelRequest(
                model="claude-model",
                system_prompt="Answer.",
                messages=(Message.user("hello"),),
                tools=(),
                tool_choice=ToolChoice.AUTO,
            )
        )

    invalid_runner = FakeRunner(
        CLIProcessOutput(
            returncode=0,
            stdout=json.dumps(
                {
                    "structured_output": {
                        "content": None,
                        "tool_calls": [
                            {
                                "name": "weather",
                                "arguments_json": "{}",
                            }
                        ],
                    }
                }
            ),
            stderr="",
        )
    )
    provider = ClaudeCLIProvider(runner=invalid_runner)
    with pytest.raises(
        InvalidProviderResponseError,
        match="tool_choice='none'",
    ):
        await provider.complete(_request(tool_choice=ToolChoice.NONE))


async def test_subprocess_runner_enforces_executable_timeout_and_bounds(
    tmp_path: Path,
) -> None:
    with pytest.raises(CLIExecutableNotFoundError, match="not found"):
        await run_cli_process(
            ("definitely-not-a-real-base-agent-command",),
            "",
            tmp_path,
            1,
            100,
            None,
        )

    with pytest.raises(CLIProcessTimeoutError, match="timeout"):
        await run_cli_process(
            (sys.executable, "-c", "import time; time.sleep(1)"),
            "",
            tmp_path,
            0.01,
            100,
            None,
        )

    with pytest.raises(CLIOutputLimitError, match="stdout"):
        await run_cli_process(
            (sys.executable, "-c", "print('x' * 1000)"),
            "",
            tmp_path,
            1,
            100,
            None,
        )
