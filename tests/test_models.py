import pytest
from pydantic import ValidationError

from base_agent import (
    AgentResult,
    AgentResultStatus,
    Message,
    ModelRequest,
    ModelResponse,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolDefinition,
)


def test_message_round_trip_preserves_tool_call_contract() -> None:
    message = Message.assistant(
        tool_calls=(ToolCall(id="call-1", name="weather.get", arguments={"city": "上海"}),)
    )

    restored = Message.model_validate_json(message.model_dump_json())

    assert restored == message
    assert restored.tool_calls[0].arguments == {"city": "上海"}


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Message.assistant(),
        lambda: Message(role="user", content="hello", tool_call_id="call-1"),
        lambda: Message(role="tool", content="result"),
        lambda: Message(
            role="user",
            content="hello",
            tool_calls=(ToolCall(id="call-1", name="demo", arguments={}),),
        ),
    ],
)
def test_invalid_message_shapes_are_rejected(factory: object) -> None:
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_required_tool_choice_needs_a_tool_definition() -> None:
    with pytest.raises(ValidationError, match="requires at least one tool"):
        ModelRequest(messages=(Message.user("hello"),), tool_choice=ToolChoice.REQUIRED)


def test_model_request_and_response_round_trip() -> None:
    request = ModelRequest(
        messages=(Message.system("Be concise."), Message.user("Weather?")),
        tools=(
            ToolDefinition(
                name="weather.get",
                description="Get current weather",
                input_schema={
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            ),
        ),
    )
    response = ModelResponse(
        tool_calls=(ToolCall(id="call-1", name="weather.get", arguments={"city": "上海"}),),
        usage=TokenUsage(input_tokens=10, output_tokens=4),
    )

    restored_request = ModelRequest.model_validate_json(request.model_dump_json())
    restored_response = ModelResponse.model_validate_json(response.model_dump_json())

    assert restored_request == request
    assert restored_response == response
    assert restored_response.usage.total_tokens == 14
    assert restored_response.to_assistant_message().tool_calls == response.tool_calls


def test_agent_result_is_provider_independent_and_serializable() -> None:
    result = AgentResult(
        status=AgentResultStatus.COMPLETED,
        output="done",
        messages=(Message.user("work"), Message.assistant("done")),
        usage=TokenUsage(input_tokens=3, output_tokens=1),
    )

    restored = AgentResult.model_validate_json(result.model_dump_json())

    assert restored == result
    assert restored.usage.total_tokens == 4


def test_token_usage_rejects_an_inconsistent_total() -> None:
    with pytest.raises(ValidationError, match="must equal"):
        TokenUsage(input_tokens=3, output_tokens=2, total_tokens=99)
