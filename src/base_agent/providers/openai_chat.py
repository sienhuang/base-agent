"""Optional OpenAI-compatible Chat Completions adapter."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import import_module
from typing import Any, Protocol, cast

from base_agent.models import (
    Message,
    MessageRole,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)
from base_agent.providers.errors import (
    InvalidProviderResponseError,
    MissingProviderDependencyError,
    UnsupportedAttachmentError,
    UnsupportedMemoryError,
)


class _CompletionCreator(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _ChatResource(Protocol):
    @property
    def completions(self) -> _CompletionCreator: ...


class OpenAIChatClient(Protocol):
    """Small SDK surface used by OpenAIChatProvider and test doubles."""

    @property
    def chat(self) -> _ChatResource: ...


class OpenAIChatProvider:
    """Map core models to an OpenAI-compatible Chat Completions client."""

    def __init__(
        self,
        *,
        model: str,
        client: OpenAIChatClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        temperature: float | None = None,
        max_completion_tokens: int | None = None,
        name: str = "openai-chat",
    ) -> None:
        if not model:
            raise ValueError("OpenAIChatProvider model must not be empty")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        if max_retries is not None and max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if max_completion_tokens is not None and max_completion_tokens <= 0:
            raise ValueError("max_completion_tokens must be greater than zero")

        self._name = name
        self.model = model
        self.temperature = temperature
        self.max_completion_tokens = max_completion_tokens
        self.client = client or self._create_client(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    @property
    def name(self) -> str:
        return self._name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if request.attachments:
            raise UnsupportedAttachmentError(
                "OpenAIChatProvider does not map attachments; use an attachment-capable "
                "Provider or process them through Tools"
            )
        if request.memories:
            raise UnsupportedMemoryError(
                "OpenAIChatProvider does not map retrieved memories; use a memory-capable "
                "Provider or retrieve them through Tools"
            )
        parameters: dict[str, Any] = {
            "model": request.model or self.model,
            "messages": [_message_to_openai(message) for message in request.messages],
        }
        if request.tools:
            parameters["tools"] = [_tool_to_openai(tool) for tool in request.tools]
            parameters["tool_choice"] = request.tool_choice.value
        elif request.tool_choice is ToolChoice.NONE:
            parameters["tool_choice"] = ToolChoice.NONE.value
        if self.temperature is not None:
            parameters["temperature"] = self.temperature
        if self.max_completion_tokens is not None:
            parameters["max_completion_tokens"] = self.max_completion_tokens

        completion = await self.client.chat.completions.create(**parameters)
        return _response_from_openai(completion)

    @staticmethod
    def _create_client(
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float | None,
        max_retries: int | None,
    ) -> OpenAIChatClient:
        try:
            openai_module = import_module("openai")
            async_openai = openai_module.AsyncOpenAI
        except ImportError as exc:
            raise MissingProviderDependencyError(
                "OpenAIChatProvider requires the optional dependency: "
                "install 'base-agent[openai]'"
            ) from exc

        options: dict[str, Any] = {}
        if api_key is not None:
            options["api_key"] = api_key
        if base_url is not None:
            options["base_url"] = base_url
        if timeout is not None:
            options["timeout"] = timeout
        if max_retries is not None:
            options["max_retries"] = max_retries
        return cast(OpenAIChatClient, async_openai(**options))


def _message_to_openai(message: Message) -> dict[str, Any]:
    if message.role is MessageRole.ASSISTANT:
        payload: dict[str, Any] = {"role": "assistant", "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return payload
    if message.role is MessageRole.TOOL:
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    return {"role": message.role.value, "content": message.content}


def _tool_to_openai(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _response_from_openai(completion: Any) -> ModelResponse:
    choices = getattr(completion, "choices", None)
    if not choices:
        raise InvalidProviderResponseError("OpenAI-compatible response has no choices")

    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise InvalidProviderResponseError("OpenAI-compatible response choice has no message")

    tool_calls = tuple(
        _tool_call_from_openai(call) for call in (getattr(message, "tool_calls", None) or ())
    )
    content = getattr(message, "content", None)
    refusal = getattr(message, "refusal", None)
    normalized_content = content if content is not None else refusal
    if normalized_content is None and not tool_calls:
        raise InvalidProviderResponseError(
            "OpenAI-compatible response message has neither content nor tool_calls"
        )

    metadata = _compact_mapping(
        {
            "id": getattr(completion, "id", None),
            "model": getattr(completion, "model", None),
            "request_id": getattr(completion, "_request_id", None),
            "system_fingerprint": getattr(completion, "system_fingerprint", None),
            "refusal": refusal,
        }
    )
    return ModelResponse(
        content=normalized_content,
        tool_calls=tool_calls,
        finish_reason=getattr(choice, "finish_reason", None),
        usage=_usage_from_openai(getattr(completion, "usage", None)),
        provider_metadata=metadata,
    )


def _tool_call_from_openai(call: Any) -> ToolCall:
    function = getattr(call, "function", None)
    if function is None:
        raise InvalidProviderResponseError("tool call has no function payload")
    raw_arguments = getattr(function, "arguments", "{}") or "{}"
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        raise InvalidProviderResponseError(
            f"tool call '{getattr(call, 'id', '')}' contains invalid JSON arguments"
        ) from exc
    if not isinstance(arguments, dict):
        raise InvalidProviderResponseError("tool call arguments must decode to a JSON object")
    return ToolCall(
        id=str(getattr(call, "id", "")),
        name=str(getattr(function, "name", "")),
        arguments=arguments,
    )


def _usage_from_openai(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    return TokenUsage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
    )


def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}
