import pytest

from base_agent import Message, ModelProvider, ModelRequest, ModelResponse, ToolCall
from base_agent.testing import FakeModel, FakeModelExhaustedError


def test_fake_model_implements_provider_protocol() -> None:
    fake = FakeModel([ModelResponse(content="hello")])

    assert isinstance(fake, ModelProvider)
    assert fake.name == "fake-model"


@pytest.mark.asyncio
async def test_fake_model_returns_scripted_text_and_tool_calls() -> None:
    responses = [
        ModelResponse(content="thinking"),
        ModelResponse(tool_calls=(ToolCall(id="call-1", name="demo", arguments={}),)),
    ]
    fake = FakeModel(responses)
    request = ModelRequest(messages=(Message.user("start"),))

    first = await fake.complete(request)
    second = await fake.complete(request)

    assert first.content == "thinking"
    assert second.tool_calls[0].name == "demo"
    assert fake.requests == (request, request)
    assert fake.remaining_responses == 0


@pytest.mark.asyncio
async def test_fake_model_fails_loudly_when_script_is_exhausted() -> None:
    fake = FakeModel([])
    request = ModelRequest(messages=(Message.user("start"),))

    with pytest.raises(FakeModelExhaustedError, match="no scripted responses"):
        await fake.complete(request)

    assert fake.requests == (request,)
