"""Application-owned Provider selection for the first SDK capability test."""

import asyncio
import json
import re
from collections.abc import AsyncIterator

from base_agent.models import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ModelToolCallDelta,
    ToolCall,
)
from base_agent.providers import (
    ClaudeCLIProvider,
    CodexCLIProvider,
    ModelProvider,
    OpenAIChatProvider,
    ProviderCapabilities,
    ProviderInfo,
)

from agent_app.config import Settings


class OfflineProvider:
    @property
    def info(self) -> ProviderInfo:
        return ProviderInfo(
            name="starter-offline",
            default_model="starter-offline",
            capabilities=ProviderCapabilities(
                tools=True,
                streaming=True,
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        user_messages = [message.content for message in request.messages if message.role == "user"]
        if not user_messages:
            raise ValueError("OfflineProvider requires one user message")
        tool_messages = [message for message in request.messages if message.role == "tool"]
        if tool_messages:
            if _show_offline_observations(user_messages[-1]):
                return ModelResponse(content=_offline_observation_details(tool_messages))
            statuses = [_observation_status(message.content) for message in tool_messages]
            return ModelResponse(
                content=(
                    f"executed={statuses.count('success')}, "
                    f"not_found={statuses.count('not_found')}, "
                    f"denied={statuses.count('denied')}, "
                    f"confirmation_denied="
                    f"{statuses.count('confirmation_denied')}, "
                    f"invalid_arguments="
                    f"{statuses.count('invalid_arguments')}, "
                    f"error={statuses.count('error')}, "
                    f"timeout={statuses.count('timeout')}, "
                    f"limit_exceeded={statuses.count('limit_exceeded')}"
                )
            )

        requested_calls = _requested_offline_tool_calls(user_messages[-1])
        if requested_calls is not None and request.tools:
            tool_name = _offline_tool_name(user_messages[-1])
            explicit_arguments = _offline_tool_arguments(user_messages[-1])
            return ModelResponse(
                tool_calls=tuple(
                    ToolCall(
                        id=f"call-{index}",
                        name=tool_name,
                        arguments=(
                            dict(explicit_arguments)
                            if explicit_arguments is not None
                            else _default_tool_arguments(tool_name, index)
                        ),
                    )
                    for index in range(1, requested_calls + 1)
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            )
        return ModelResponse(content=f"Starter Agent received: {user_messages[-1]}")

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        response = await self.complete(request)
        if response.content:
            for offset in range(0, len(response.content), 8):
                await asyncio.sleep(0)
                yield ModelTextDelta(response.content[offset : offset + 8])
        for index, call in enumerate(response.tool_calls):
            encoded_arguments = json.dumps(
                dict(call.arguments),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            split_at = max(1, len(encoded_arguments) // 2)
            fragments = (
                encoded_arguments[:split_at],
                encoded_arguments[split_at:],
            )
            await asyncio.sleep(0)
            yield ModelToolCallDelta(
                index=index,
                id=call.id,
                name=call.name,
                arguments_delta=fragments[0],
            )
            if fragments[1]:
                await asyncio.sleep(0)
                yield ModelToolCallDelta(
                    index=index,
                    arguments_delta=fragments[1],
                )
        yield ModelStreamCompleted(response)


def build_provider(
    settings: Settings | None = None,
) -> ModelProvider:
    """Build one Provider without exposing credentials to the Agent definition."""
    resolved = settings or Settings()
    if resolved.provider == "offline":
        return OfflineProvider()
    if resolved.provider == "codex-cli":
        return CodexCLIProvider(
            model=resolved.model,
            executable=resolved.cli_executable or "codex",
            timeout_seconds=resolved.cli_timeout_seconds,
            max_output_bytes=resolved.cli_max_output_bytes,
        )
    if resolved.provider == "claude-cli":
        return ClaudeCLIProvider(
            model=resolved.model,
            executable=resolved.cli_executable or "claude",
            timeout_seconds=resolved.cli_timeout_seconds,
            max_output_bytes=resolved.cli_max_output_bytes,
        )
    if resolved.model is None:
        raise ValueError("OpenAI-compatible mode requires --model, AGENT_MODEL, or OPENAI_MODEL")
    if resolved.api_key is None:
        raise ValueError(
            "OpenAI-compatible mode requires OPENAI_API_KEY; "
            "use a placeholder value if the local server does not authenticate"
        )

    return OpenAIChatProvider(
        default_model=resolved.model,
        api_key=resolved.api_key,
        base_url=resolved.base_url,
        timeout_seconds=60,
    )


def _requested_offline_tool_calls(prompt: str) -> int | None:
    matched = re.search(r"\bcalls\s*=\s*(\d+)\b", prompt, flags=re.IGNORECASE)
    if matched is None:
        return None
    requested = int(matched.group(1))
    if requested < 1:
        raise ValueError("calls must be at least 1")
    return requested


def _offline_tool_name(prompt: str) -> str:
    matched = re.search(
        r"\btool\s*=\s*([A-Za-z0-9_.-]+)\b",
        prompt,
        flags=re.IGNORECASE,
    )
    return matched.group(1) if matched is not None else "record_value"


def _default_tool_arguments(
    tool_name: str,
    call_index: int,
) -> dict[str, object]:
    if tool_name == "record_value":
        return {"value": call_index}
    return {}


def _offline_tool_arguments(prompt: str) -> dict[str, object] | None:
    marker = "tool_args="
    marker_index = prompt.find(marker)
    if marker_index < 0:
        return None
    encoded = prompt[marker_index + len(marker) :].lstrip()
    try:
        value, _ = json.JSONDecoder().raw_decode(encoded)
    except json.JSONDecodeError as exc:
        raise ValueError("tool_args must contain a valid JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("tool_args must contain a JSON object")
    return value


def _show_offline_observations(prompt: str) -> bool:
    return (
        re.search(
            r"\bshow_observations\s*=\s*true\b",
            prompt,
            flags=re.IGNORECASE,
        )
        is not None
    )


def _offline_observation_details(messages: list[Message]) -> str:
    observations: list[dict[str, object]] = []
    for message in messages:
        if message.tool_call_id is None or message.name is None:
            raise ValueError("Tool observation must identify its ToolCall and Tool")
        observations.append(
            {
                "tool_call_id": message.tool_call_id,
                "name": message.name,
                "content": _observation_payload(message.content),
            }
        )
    return json.dumps(
        {"observations": observations},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _observation_payload(content: str) -> dict[str, object]:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("Tool observation must be a JSON object")
    return value


def _observation_status(content: str) -> str:
    value = _observation_payload(content)
    status = value.get("status")
    if not isinstance(status, str):
        raise ValueError("Tool observation must contain a string status")
    return status
