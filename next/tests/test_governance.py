from base_agent import (
    Agent,
    AgentDefinition,
    Confirmation,
    RunStatus,
    SideEffect,
    tool,
)
from base_agent.models import ModelResponse, ToolCall
from base_agent.testing import AllowAllConfirmations, FakeModel


def _mutating_model() -> FakeModel:
    return FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        name="save_report",
                        arguments={"content": "report"},
                    ),
                )
            ),
            ModelResponse(content="saved"),
        ]
    )


async def test_side_effect_confirmation_is_denied_by_default() -> None:
    called = False

    @tool(
        side_effect=SideEffect.MUTATING,
        confirmation=Confirmation.REQUIRED,
    )
    async def save_report(content: str) -> str:
        nonlocal called
        called = True
        return content

    agent = Agent(
        AgentDefinition(
            id="writer",
            version="1.0.0",
            instructions="Write.",
            tools=("save_report",),
        ),
        model=_mutating_model(),
        tools=(save_report,),
    )

    result = await agent.run("save")

    assert result.status is RunStatus.SUCCEEDED
    assert called is False
    assert result.tool_results[0].status.value == "confirmation_denied"


async def test_application_can_explicitly_confirm_side_effect() -> None:
    @tool(
        side_effect=SideEffect.MUTATING,
        confirmation=Confirmation.REQUIRED,
    )
    async def save_report(content: str) -> str:
        return content

    agent = Agent(
        AgentDefinition(
            id="writer",
            version="1.0.0",
            instructions="Write.",
            tools=("save_report",),
        ),
        model=_mutating_model(),
        tools=(save_report,),
        confirmation_policy=AllowAllConfirmations(),
    )

    result = await agent.run("save")

    assert result.status is RunStatus.SUCCEEDED
    assert result.tool_results[0].status.value == "success"
