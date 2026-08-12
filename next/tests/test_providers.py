from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from base_agent import Agent, AgentDefinition, tool
from base_agent.models import (
    FinishReason,
    Message,
    ModelRequest,
    ModelResponse,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCallDelta,
    ToolCall,
    ToolChoice,
    ToolSpec,
)
from base_agent.providers import (
    InvalidProviderResponseError,
    MissingProviderDependencyError,
    ModelProvider,
    OpenAIChatProvider,
    ProviderCapabilities,
    ProviderInfo,
    ProviderRateLimitError,
    openai_chat,
)
from base_agent.testing import FakeModel


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


class FakeOpenAIClient:
    def __init__(self, responses: list[Any]) -> None:
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeAsyncStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = iter(chunks)
        self._request_id = "stream-request-1"

    def __aiter__(self) -> "FakeAsyncStream":
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _completion(
    *,
    content: str | None = "done",
    finish_reason: str = "stop",
    tool_calls: list[Any] | None = None,
    usage: Any = None,
) -> Any:
    return SimpleNamespace(
        id="chatcmpl-1",
        model="served-model",
        system_fingerprint="fp-1",
        _request_id="request-1",
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=content,
                    refusal=None,
                    tool_calls=tool_calls,
                ),
            )
        ],
        usage=usage,
    )


def _stream_chunk(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
    choices: bool = True,
) -> Any:
    normalized_choices = (
        [
            SimpleNamespace(
                finish_reason=finish_reason,
                delta=SimpleNamespace(
                    content=content,
                    refusal=None,
                    tool_calls=tool_calls,
                ),
            )
        ]
        if choices
        else []
    )
    return SimpleNamespace(
        id="chatcmpl-stream-1",
        model="served-stream-model",
        system_fingerprint="stream-fp-1",
        choices=normalized_choices,
        usage=usage,
    )


def _request(**changes: Any) -> ModelRequest:
    values: dict[str, Any] = {
        "model": "requested-model",
        "system_prompt": "Be concise.",
        "messages": (Message.user("hello", message_id="runtime-message-1"),),
        "tools": (),
        "tool_choice": ToolChoice.AUTO,
        "response_schema": None,
    }
    values.update(changes)
    return ModelRequest(**values)


async def test_harness_resolves_provider_and_model_identity_into_snapshot() -> None:
    model = FakeModel(
        [ModelResponse(content="ok")],
        name="fake-provider",
        model="fake-model-v1",
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=model,
    )

    result = await agent.run("hello")

    assert isinstance(model, ModelProvider)
    assert result.snapshot.provider_name == "fake-provider"
    assert result.snapshot.model_name == "fake-model-v1"
    assert model.requests[0].model == "fake-model-v1"


async def test_agent_definition_can_override_provider_default_model() -> None:
    model = FakeModel(
        [ModelResponse(content="ok")],
        name="fake-provider",
        model="default-model",
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer.",
            model="explicit-model",
        ),
        model=model,
    )

    result = await agent.run("hello")

    assert result.snapshot.model_name == "explicit-model"
    assert model.requests[0].model == "explicit-model"


def test_agent_rejects_provider_without_required_capability() -> None:
    @tool
    async def lookup(query: str) -> str:
        return query

    model = FakeModel(
        [ModelResponse(content="unused")],
        capabilities=ProviderCapabilities(),
    )

    with pytest.raises(ValueError, match="does not support Tool calls"):
        Agent(
            AgentDefinition(
                id="assistant",
                version="1.0.0",
                instructions="Use the Tool.",
                tools=("lookup",),
            ),
            model=model,
            tools=(lookup,),
        )


def test_agent_rejects_unsupported_structured_output() -> None:
    class Output(BaseModel):
        answer: str

    model = FakeModel(
        [ModelResponse(content='{"answer":"unused"}')],
        capabilities=ProviderCapabilities(),
    )

    with pytest.raises(ValueError, match="does not support structured output"):
        Agent(
            AgentDefinition(
                id="assistant",
                version="1.0.0",
                instructions="Return JSON.",
                response_model=Output,
            ),
            model=model,
        )


async def test_openai_adapter_maps_request_response_usage_and_metadata() -> None:
    usage = SimpleNamespace(
        prompt_tokens=12,
        completion_tokens=4,
        total_tokens=16,
        prompt_tokens_details=SimpleNamespace(cached_tokens=5),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
    )
    client = FakeOpenAIClient([_completion(content='{"answer":"ok"}', usage=usage)])
    provider = OpenAIChatProvider(
        default_model="default-model",
        client=client,
        temperature=0.2,
        max_output_tokens=500,
    )
    request = _request(
        tools=(
            ToolSpec(
                name="lookup",
                description="Look up data",
                input_schema={"type": "object", "properties": {}},
            ),
        ),
        tool_choice=ToolChoice.REQUIRED,
        response_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    )

    response = await provider.complete(request)
    sent = client.completions.requests[0]

    assert provider.info == ProviderInfo(
        name="openai-compatible",
        default_model="default-model",
        capabilities=ProviderCapabilities(
            tools=True,
            structured_output=True,
            streaming=True,
        ),
    )
    assert sent["model"] == "requested-model"
    assert sent["messages"][0] == {
        "role": "system",
        "content": "Be concise.",
    }
    assert sent["messages"][1] == {"role": "user", "content": "hello"}
    assert all("message_id" not in message for message in sent["messages"])
    assert sent["tools"][0]["function"]["name"] == "lookup"
    assert sent["tool_choice"] == "required"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["temperature"] == 0.2
    assert sent["max_completion_tokens"] == 500
    assert response.finish_reason is FinishReason.STOP
    assert response.usage.total_tokens == 16
    assert response.usage.cached_input_tokens == 5
    assert response.usage.reasoning_tokens == 2
    assert response.provider_data["request_id"] == "request-1"
    assert response.provider_data["model"] == "served-model"


async def test_openai_adapter_streams_text_and_completes_response() -> None:
    usage = SimpleNamespace(
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
        prompt_tokens_details=None,
        completion_tokens_details=None,
    )
    raw_stream = FakeAsyncStream(
        [
            _stream_chunk(content="Hel"),
            _stream_chunk(content="lo", finish_reason="stop"),
            _stream_chunk(choices=False, usage=usage),
        ]
    )
    client = FakeOpenAIClient([raw_stream])
    provider = OpenAIChatProvider(
        default_model="test-model",
        client=client,
    )

    events = [event async for event in provider.stream(_request())]

    assert client.completions.requests[0]["stream"] is True
    assert events[:2] == [ModelTextDelta("Hel"), ModelTextDelta("lo")]
    assert isinstance(events[-1], ModelStreamCompleted)
    assert events[-1].response.content == "Hello"
    assert events[-1].response.finish_reason is FinishReason.STOP
    assert events[-1].response.usage.total_tokens == 5
    assert events[-1].response.provider_data["request_id"] == ("stream-request-1")


async def test_openai_adapter_assembles_streamed_tool_call_arguments() -> None:
    first_delta = SimpleNamespace(
        index=0,
        id="call-stream-1",
        function=SimpleNamespace(
            name="lookup",
            arguments='{"query":',
        ),
    )
    second_delta = SimpleNamespace(
        index=0,
        id=None,
        function=SimpleNamespace(
            # Some compatible Providers resend the complete atomic name.
            name="lookup",
            arguments='"上海"}',
        ),
    )
    provider = OpenAIChatProvider(
        default_model="test-model",
        client=FakeOpenAIClient(
            [
                FakeAsyncStream(
                    [
                        _stream_chunk(tool_calls=[first_delta]),
                        _stream_chunk(
                            tool_calls=[second_delta],
                            finish_reason="tool_calls",
                        ),
                    ]
                )
            ]
        ),
    )

    events = [event async for event in provider.stream(_request())]

    assert events[:2] == [
        ModelToolCallDelta(
            index=0,
            id="call-stream-1",
            name="lookup",
            arguments_delta='{"query":',
        ),
        ModelToolCallDelta(
            index=0,
            name="lookup",
            arguments_delta='"上海"}',
        ),
    ]
    assert isinstance(events[-1], ModelStreamCompleted)
    assert events[-1].response.tool_calls == (
        ToolCall(
            id="call-stream-1",
            name="lookup",
            arguments={"query": "上海"},
        ),
    )
    assert events[-1].response.finish_reason is FinishReason.TOOL_CALLS


async def test_openai_adapter_normalizes_tool_calls() -> None:
    tool_call = SimpleNamespace(
        id="call-1",
        function=SimpleNamespace(
            name="lookup",
            arguments='{"query":"上海"}',
        ),
    )
    provider = OpenAIChatProvider(
        default_model="test-model",
        client=FakeOpenAIClient(
            [
                _completion(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[tool_call],
                )
            ]
        ),
    )

    response = await provider.complete(_request())

    assert response.finish_reason is FinishReason.TOOL_CALLS
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].arguments == {"query": "上海"}


async def test_openai_adapter_translates_rate_limit_errors() -> None:
    class UpstreamRateLimitError(RuntimeError):
        status_code = 429

    error = UpstreamRateLimitError("too many requests")
    provider = OpenAIChatProvider(
        default_model="test-model",
        client=FakeOpenAIClient([error]),
    )

    with pytest.raises(ProviderRateLimitError) as captured:
        await provider.complete(_request())

    assert captured.value.retryable is True
    assert captured.value.status_code == 429
    assert captured.value.provider == "openai-compatible"


async def test_harness_preserves_normalized_provider_failure() -> None:
    class UpstreamRateLimitError(RuntimeError):
        status_code = 429

    provider = OpenAIChatProvider(
        default_model="test-model",
        client=FakeOpenAIClient([UpstreamRateLimitError("too many requests")]),
    )
    agent = Agent(
        AgentDefinition(
            id="assistant",
            version="1.0.0",
            instructions="Answer.",
        ),
        model=provider,
    )

    result = await agent.run("hello")

    assert result.failure is not None
    assert result.failure.code == "provider_rate_limit"
    assert result.failure.retryable is True
    assert result.failure.details["status_code"] == 429
    assert result.events[-2].kind == "model.failed"
    assert result.events[-2].payload["error_code"] == "provider_rate_limit"


@pytest.mark.parametrize(
    "completion",
    [
        SimpleNamespace(choices=[]),
        _completion(content=None, tool_calls=[]),
        _completion(
            content=None,
            tool_calls=[
                SimpleNamespace(
                    id="call-1",
                    function=SimpleNamespace(
                        name="lookup",
                        arguments="not-json",
                    ),
                )
            ],
        ),
    ],
)
async def test_openai_adapter_rejects_invalid_responses(completion: Any) -> None:
    provider = OpenAIChatProvider(
        default_model="test-model",
        client=FakeOpenAIClient([completion]),
    )

    with pytest.raises(InvalidProviderResponseError):
        await provider.complete(_request())


def test_missing_openai_dependency_has_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_import(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(openai_chat, "import_module", missing_import)

    with pytest.raises(
        MissingProviderDependencyError,
        match=r"base-agent-next\[openai\]",
    ):
        OpenAIChatProvider(default_model="test-model")
