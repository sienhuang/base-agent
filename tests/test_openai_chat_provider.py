from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from base_agent import (
    Agent,
    AgentProfile,
    AgentResultStatus,
    Message,
    ModelProvider,
    ModelRequest,
    OpenAIChatProvider,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    tool,
)
from base_agent.providers import (
    InvalidProviderResponseError,
    MissingProviderDependencyError,
    openai_chat,
)


class FakeCompletions:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return self.responses.popleft()


class FakeOpenAIClient:
    def __init__(self, responses: list[Any]) -> None:
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


def _sdk_tool_call(call_id: str, name: str, arguments: str) -> Any:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _completion(
    *,
    content: str | None,
    tool_calls: list[Any] | None = None,
    finish_reason: str = "stop",
    usage: Any = None,
    request_id: str | None = None,
) -> Any:
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None)
    return SimpleNamespace(
        id="chatcmpl-1",
        model="response-model",
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        usage=usage,
        system_fingerprint="fp-1",
        _request_id=request_id,
    )


@pytest.mark.asyncio
async def test_provider_maps_messages_tools_options_usage_and_metadata() -> None:
    completion = _completion(
        content=None,
        tool_calls=[_sdk_tool_call("call-2", "weather", '{"city":"上海"}')],
        finish_reason="tool_calls",
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=4, total_tokens=16),
        request_id="req-123",
    )
    client = FakeOpenAIClient([completion])
    provider = OpenAIChatProvider(
        model="default-model",
        client=client,
        temperature=0.2,
        max_completion_tokens=500,
    )
    prior_call = ToolCall(id="call-1", name="lookup", arguments={"query": "天气"})
    request = ModelRequest(
        model="request-model",
        messages=(
            Message.system("Be concise."),
            Message.user("Weather?"),
            Message.assistant(tool_calls=(prior_call,)),
            Message.tool('{"status":"success"}', tool_call_id="call-1"),
        ),
        tools=(
            ToolDefinition(
                name="weather",
                description="Get weather",
                input_schema={"type": "object", "properties": {}},
            ),
        ),
        tool_choice=ToolChoice.REQUIRED,
    )

    response = await provider.complete(request)
    sent = client.completions.requests[0]

    assert isinstance(provider, ModelProvider)
    assert sent["model"] == "request-model"
    assert sent["tool_choice"] == "required"
    assert sent["temperature"] == 0.2
    assert sent["max_completion_tokens"] == 500
    assert sent["messages"][2]["tool_calls"][0]["function"]["arguments"] == (
        '{"query":"天气"}'
    )
    assert sent["messages"][3] == {
        "role": "tool",
        "content": '{"status":"success"}',
        "tool_call_id": "call-1",
    }
    assert sent["tools"][0]["function"]["parameters"]["type"] == "object"
    assert response.tool_calls[0].arguments == {"city": "上海"}
    assert response.usage.total_tokens == 16
    assert response.finish_reason == "tool_calls"
    assert response.provider_metadata["request_id"] == "req-123"
    assert response.provider_metadata["model"] == "response-model"


@pytest.mark.asyncio
async def test_auto_tool_choice_is_omitted_when_no_tools_are_present() -> None:
    client = FakeOpenAIClient([_completion(content="done")])
    provider = OpenAIChatProvider(model="default-model", client=client)

    response = await provider.complete(ModelRequest(messages=(Message.user("hello"),)))

    assert response.content == "done"
    assert "tools" not in client.completions.requests[0]
    assert "tool_choice" not in client.completions.requests[0]


@pytest.mark.asyncio
async def test_none_tool_choice_is_forwarded_without_tool_definitions() -> None:
    client = FakeOpenAIClient([_completion(content="done")])
    provider = OpenAIChatProvider(model="default-model", client=client)

    await provider.complete(
        ModelRequest(
            messages=(Message.user("hello"),),
            tool_choice=ToolChoice.NONE,
        )
    )

    assert client.completions.requests[0]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_provider_executes_full_agent_tool_loop_with_fake_sdk_client() -> None:
    @tool
    async def weather(city: str) -> str:
        return f"{city}: sunny"

    client = FakeOpenAIClient(
        [
            _completion(
                content=None,
                tool_calls=[_sdk_tool_call("call-1", "weather", '{"city":"上海"}')],
                finish_reason="tool_calls",
            ),
            _completion(content="上海天气晴朗。"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="assistant",
            instructions="Use the weather tool.",
            tools=("weather",),
        ),
        model=OpenAIChatProvider(model="test-model", client=client),
        tools=[weather],
    )

    result = await agent.run("上海天气怎么样？")

    assert result.status is AgentResultStatus.COMPLETED
    assert result.output == "上海天气晴朗。"
    assert len(client.completions.requests) == 2
    assert client.completions.requests[1]["messages"][-1]["role"] == "tool"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion", "message"),
    [
        (SimpleNamespace(choices=[]), "no choices"),
        (
            _completion(
                content=None,
                tool_calls=[_sdk_tool_call("call-1", "demo", "not-json")],
            ),
            "invalid JSON arguments",
        ),
        (
            _completion(
                content=None,
                tool_calls=[_sdk_tool_call("call-1", "demo", "[]")],
            ),
            "JSON object",
        ),
    ],
)
async def test_invalid_provider_responses_fail_at_adapter_boundary(
    completion: Any,
    message: str,
) -> None:
    provider = OpenAIChatProvider(
        model="test-model",
        client=FakeOpenAIClient([completion]),
    )

    with pytest.raises(InvalidProviderResponseError, match=message):
        await provider.complete(ModelRequest(messages=(Message.user("hello"),)))


def test_provider_configuration_rejects_invalid_values() -> None:
    client = FakeOpenAIClient([])

    with pytest.raises(ValueError, match="model must not be empty"):
        OpenAIChatProvider(model="", client=client)
    with pytest.raises(ValueError, match="timeout"):
        OpenAIChatProvider(model="test", client=client, timeout=0)
    with pytest.raises(ValueError, match="max_retries"):
        OpenAIChatProvider(model="test", client=client, max_retries=-1)


def test_missing_optional_sdk_has_an_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_import(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(openai_chat, "import_module", missing_import)

    with pytest.raises(MissingProviderDependencyError, match=r"base-agent\[openai\]"):
        OpenAIChatProvider(model="test-model")
