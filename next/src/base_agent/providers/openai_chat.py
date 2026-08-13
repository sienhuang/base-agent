"""Optional OpenAI-compatible Chat Completions Adapter."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from importlib import import_module
from typing import Any, Literal, Protocol, cast

from base_agent.models import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelStreamEvent,
    ModelTextDelta,
    ModelToolCallDelta,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolSpec,
)
from base_agent.providers.errors import (
    InvalidProviderResponseError,
    MissingProviderDependencyError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from base_agent.providers.models import ProviderCapabilities, ProviderInfo

type OpenAIStructuredOutputMode = Literal[
    "json-schema",
    "json-object",
    "prompt",
]

_STRUCTURED_OUTPUT_MODES = frozenset(
    {
        "json-schema",
        "json-object",
        "prompt",
    }
)


class _CompletionCreator(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _ChatResource(Protocol):
    @property
    def completions(self) -> _CompletionCreator: ...


class OpenAIChatClient(Protocol):
    @property
    def chat(self) -> _ChatResource: ...


class OpenAIChatProvider:
    """Translate the core Provider contract to Chat Completions."""

    def __init__(
        self,
        *,
        default_model: str,
        client: OpenAIChatClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        structured_output_mode: OpenAIStructuredOutputMode = "json-schema",
        name: str = "openai-compatible",
    ) -> None:
        if not default_model.strip():
            raise ValueError("default_model must not be blank")
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_output_tokens is not None and max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")
        if structured_output_mode not in _STRUCTURED_OUTPUT_MODES:
            raise ValueError(
                "structured_output_mode must be json-schema, json-object, or prompt"
            )
        self._info = ProviderInfo(
            name=name,
            default_model=default_model,
            capabilities=ProviderCapabilities(
                tools=True,
                structured_output=True,
                streaming=True,
            ),
        )
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._structured_output_mode = structured_output_mode
        self._client = client or self._create_client(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )

    @property
    def info(self) -> ProviderInfo:
        return self._info

    async def complete(self, request: ModelRequest) -> ModelResponse:
        parameters = self._request_parameters(request)

        try:
            completion = await self._client.chat.completions.create(**parameters)
        except Exception as exc:
            raise _translate_openai_error(exc, provider=self.info.name) from exc
        return _response_from_openai(completion, provider=self.info.name)

    async def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]:
        parameters = self._request_parameters(request)
        parameters["stream"] = True
        try:
            raw_stream = await self._client.chat.completions.create(**parameters)
        except Exception as exc:
            raise _translate_openai_error(exc, provider=self.info.name) from exc

        if not hasattr(raw_stream, "__aiter__"):
            raise InvalidProviderResponseError(
                "OpenAI-compatible streaming response is not async iterable",
                provider=self.info.name,
            )

        content_parts: list[str] = []
        tool_calls: dict[int, dict[str, str]] = {}
        finish_reason = FinishReason.UNKNOWN
        usage = TokenUsage()
        metadata: dict[str, Any] = {
            "request_id": getattr(raw_stream, "_request_id", None),
        }

        try:
            async for chunk in raw_stream:
                metadata.update(
                    _compact_mapping(
                        {
                            "id": getattr(chunk, "id", None),
                            "model": getattr(chunk, "model", None),
                            "system_fingerprint": getattr(
                                chunk,
                                "system_fingerprint",
                                None,
                            ),
                        }
                    )
                )
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage is not None:
                    usage = _usage_from_openai(chunk_usage)
                choices = getattr(chunk, "choices", None) or ()
                if not choices:
                    continue
                choice = choices[0]
                raw_finish_reason = getattr(
                    choice,
                    "finish_reason",
                    None,
                )
                if raw_finish_reason is not None:
                    finish_reason = _finish_reason(raw_finish_reason)
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue

                text = getattr(delta, "content", None)
                if text is None:
                    text = getattr(delta, "refusal", None)
                if text:
                    normalized_text = str(text)
                    content_parts.append(normalized_text)
                    yield ModelTextDelta(normalized_text)

                for item in getattr(delta, "tool_calls", None) or ():
                    raw_index = getattr(item, "index", None)
                    if raw_index is None or int(raw_index) < 0:
                        raise InvalidProviderResponseError(
                            "OpenAI-compatible ToolCall delta has no index",
                            provider=self.info.name,
                        )
                    index = int(raw_index)
                    entry = tool_calls.setdefault(
                        index,
                        {"id": "", "name": "", "arguments": ""},
                    )
                    raw_id = getattr(item, "id", None)
                    call_id = str(raw_id) if raw_id is not None else None
                    function = getattr(item, "function", None)
                    raw_name = getattr(function, "name", None) if function is not None else None
                    name = str(raw_name) if raw_name else None
                    raw_arguments = (
                        getattr(function, "arguments", None) if function is not None else None
                    )
                    arguments_delta = str(raw_arguments) if raw_arguments else ""
                    if call_id:
                        entry["id"] = call_id
                    if name:
                        # OpenAI-compatible Providers send function names as
                        # atomic identifiers; assignment tolerates resends.
                        entry["name"] = name
                    entry["arguments"] += arguments_delta
                    if call_id or name or arguments_delta:
                        yield ModelToolCallDelta(
                            index=index,
                            id=call_id,
                            name=name,
                            arguments_delta=arguments_delta,
                        )
        except ProviderError:
            raise
        except Exception as exc:
            raise _translate_openai_error(
                exc,
                provider=self.info.name,
            ) from exc

        normalized_tool_calls = tuple(
            _tool_call_from_parts(
                call_id=entry["id"],
                name=entry["name"],
                raw_arguments=entry["arguments"] or "{}",
                provider=self.info.name,
            )
            for _, entry in sorted(tool_calls.items())
        )
        content = "".join(content_parts) or None
        if content is None and not normalized_tool_calls:
            raise InvalidProviderResponseError(
                "OpenAI-compatible stream has neither content nor Tool calls",
                provider=self.info.name,
            )
        yield ModelStreamCompleted(
            ModelResponse(
                content=content,
                tool_calls=normalized_tool_calls,
                finish_reason=finish_reason,
                usage=usage,
                provider_data=_compact_mapping(metadata),
            )
        )

    def _request_parameters(
        self,
        request: ModelRequest,
    ) -> dict[str, Any]:
        if request.tool_choice is ToolChoice.REQUIRED and not request.tools:
            raise ProviderInvalidRequestError(
                "tool_choice='required' requires at least one Tool",
                provider=self.info.name,
            )

        system_prompt = request.system_prompt
        if (
            request.response_schema is not None
            and self._structured_output_mode in {"json-object", "prompt"}
        ):
            system_prompt = _with_structured_output_instruction(
                system_prompt,
                request.response_schema,
            )

        parameters: dict[str, Any] = {
            "model": request.model,
            "messages": _messages_to_openai(
                system_prompt,
                request.messages,
            ),
        }
        if request.tools:
            parameters["tools"] = [_tool_to_openai(tool) for tool in request.tools]
            parameters["tool_choice"] = request.tool_choice.value
        elif request.tool_choice is ToolChoice.NONE:
            parameters["tool_choice"] = ToolChoice.NONE.value
        if (
            request.response_schema is not None
            and self._structured_output_mode == "json-schema"
        ):
            parameters["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "agent_response",
                    "strict": True,
                    "schema": dict(request.response_schema),
                },
            }
        elif (
            request.response_schema is not None
            and self._structured_output_mode == "json-object"
        ):
            parameters["response_format"] = {"type": "json_object"}
        if self._temperature is not None:
            parameters["temperature"] = self._temperature
        if self._max_output_tokens is not None:
            parameters["max_completion_tokens"] = self._max_output_tokens

        return parameters

    @staticmethod
    def _create_client(
        *,
        api_key: str | None,
        base_url: str | None,
        timeout_seconds: float | None,
    ) -> OpenAIChatClient:
        try:
            openai_module = import_module("openai")
            async_openai = openai_module.AsyncOpenAI
        except (AttributeError, ImportError) as exc:
            raise MissingProviderDependencyError(
                "OpenAIChatProvider requires 'base-agent-next[openai]'"
            ) from exc

        options: dict[str, Any] = {"max_retries": 0}
        if api_key is not None:
            options["api_key"] = api_key
        if base_url is not None:
            options["base_url"] = base_url
        if timeout_seconds is not None:
            options["timeout"] = timeout_seconds
        return cast(OpenAIChatClient, async_openai(**options))


def _messages_to_openai(
    system_prompt: str,
    messages: tuple[Message, ...],
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if system_prompt:
        converted.append({"role": "system", "content": system_prompt})
    converted.extend(_message_to_openai(message) for message in messages)
    return converted


def _with_structured_output_instruction(
    system_prompt: str,
    response_schema: Mapping[str, Any],
) -> str:
    schema = json.dumps(
        dict(response_schema),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    instruction = (
        "When producing the final assistant response (not a Tool call), return only "
        "one valid JSON value. Do not wrap it in Markdown or add explanatory text. "
        "The JSON must validate against this JSON Schema:\n"
        f"{schema}"
    )
    return f"{system_prompt}\n\n{instruction}" if system_prompt else instruction


def _message_to_openai(message: Message) -> dict[str, Any]:
    if message.role == "assistant":
        payload: dict[str, Any] = {
            "role": "assistant",
            "content": message.content or None,
        }
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            dict(call.arguments),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        return payload
    if message.role == "tool":
        return {
            "role": "tool",
            "content": message.content,
            "tool_call_id": message.tool_call_id,
        }
    return {"role": message.role, "content": message.content}


def _tool_to_openai(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": dict(tool.input_schema),
        },
    }


def _response_from_openai(completion: Any, *, provider: str) -> ModelResponse:
    choices = getattr(completion, "choices", None)
    if not choices:
        raise InvalidProviderResponseError(
            "OpenAI-compatible response has no choices",
            provider=provider,
        )
    choice = choices[0]
    message = getattr(choice, "message", None)
    if message is None:
        raise InvalidProviderResponseError(
            "OpenAI-compatible response choice has no message",
            provider=provider,
        )

    tool_calls = tuple(
        _tool_call_from_openai(item, provider=provider)
        for item in (getattr(message, "tool_calls", None) or ())
    )
    content = getattr(message, "content", None)
    refusal = getattr(message, "refusal", None)
    normalized_content = content if content is not None else refusal
    if normalized_content is None and not tool_calls:
        raise InvalidProviderResponseError(
            "OpenAI-compatible response has neither content nor Tool calls",
            provider=provider,
        )

    return ModelResponse(
        content=normalized_content,
        tool_calls=tool_calls,
        finish_reason=_finish_reason(getattr(choice, "finish_reason", None)),
        usage=_usage_from_openai(getattr(completion, "usage", None)),
        provider_data=_compact_mapping(
            {
                "id": getattr(completion, "id", None),
                "model": getattr(completion, "model", None),
                "request_id": getattr(completion, "_request_id", None),
                "system_fingerprint": getattr(
                    completion,
                    "system_fingerprint",
                    None,
                ),
                "refusal": refusal,
            }
        ),
    )


def _tool_call_from_openai(call: Any, *, provider: str) -> ToolCall:
    function = getattr(call, "function", None)
    call_id = str(getattr(call, "id", "") or "")
    name = str(getattr(function, "name", "") or "") if function is not None else ""
    raw_arguments = (getattr(function, "arguments", "{}") if function is not None else "{}") or "{}"
    return _tool_call_from_parts(
        call_id=call_id,
        name=name,
        raw_arguments=raw_arguments,
        provider=provider,
    )


def _tool_call_from_parts(
    *,
    call_id: str,
    name: str,
    raw_arguments: str,
    provider: str,
) -> ToolCall:
    if not call_id or not name:
        raise InvalidProviderResponseError(
            "OpenAI-compatible Tool call is missing id or function name",
            provider=provider,
        )
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidProviderResponseError(
            f"Tool call '{call_id}' contains invalid JSON arguments",
            provider=provider,
        ) from exc
    if not isinstance(arguments, dict):
        raise InvalidProviderResponseError(
            f"Tool call '{call_id}' arguments must be a JSON object",
            provider=provider,
        )
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _usage_from_openai(usage: Any) -> TokenUsage:
    if usage is None:
        return TokenUsage()
    input_details = getattr(usage, "prompt_tokens_details", None)
    output_details = getattr(usage, "completion_tokens_details", None)
    return TokenUsage(
        input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        total_tokens=int(getattr(usage, "total_tokens", 0) or 0),
        cached_input_tokens=int(getattr(input_details, "cached_tokens", 0) or 0),
        reasoning_tokens=int(getattr(output_details, "reasoning_tokens", 0) or 0),
    )


def _finish_reason(value: str | None) -> FinishReason:
    mapping = {
        "stop": FinishReason.STOP,
        "tool_calls": FinishReason.TOOL_CALLS,
        "length": FinishReason.LENGTH,
        "content_filter": FinishReason.CONTENT_FILTER,
    }
    if value is None:
        return FinishReason.UNKNOWN
    return mapping.get(value, FinishReason.UNKNOWN)


def _compact_mapping(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def _translate_openai_error(exc: Exception, *, provider: str) -> ProviderError:
    status_code = getattr(exc, "status_code", None)
    message = str(exc) or type(exc).__name__
    error_name = type(exc).__name__.lower()
    if status_code in {401, 403}:
        return ProviderAuthenticationError(
            message,
            provider=provider,
            status_code=status_code,
        )
    if status_code == 429:
        return ProviderRateLimitError(
            message,
            provider=provider,
            status_code=status_code,
        )
    if status_code in {400, 404, 409, 422}:
        return ProviderInvalidRequestError(
            message,
            provider=provider,
            status_code=status_code,
        )
    if status_code == 408 or "timeout" in error_name:
        return ProviderTimeoutError(
            message,
            provider=provider,
            status_code=status_code,
        )
    if isinstance(status_code, int) and status_code >= 500:
        return ProviderUnavailableError(
            message,
            provider=provider,
            status_code=status_code,
        )
    return ProviderError(
        message,
        provider=provider,
        status_code=status_code,
    )
