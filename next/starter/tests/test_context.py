from dataclasses import replace

from base_agent import InMemoryArtifactStore, RunStatus
from base_agent.context import ContextBudget, ContextInput, SystemContext
from base_agent.memory import MemorySnapshot
from base_agent.models import Message, ModelResponse, ToolCall
from base_agent.testing import FakeModel

from agent_app.app import STARTER_AGENT_DEFINITION, build_agent, build_plan_react_flow
from agent_app.config import Settings
from agent_app.context import (
    RecentToolGroupsContextPolicy,
    build_plan_react_context,
)


def test_recent_tool_groups_policy_keeps_complete_multi_tool_groups() -> None:
    first_calls = (
        ToolCall(id="first-a", name="lookup", arguments={}),
        ToolCall(id="first-b", name="lookup", arguments={}),
    )
    second_call = ToolCall(id="second", name="lookup", arguments={})
    transcript = (
        Message.user("start", message_id="user-start"),
        Message.assistant("", message_id="assistant-first", tool_calls=first_calls),
        Message.tool_result(first_calls[0], "a", message_id="tool-first-a"),
        Message.tool_result(first_calls[1], "b", message_id="tool-first-b"),
        Message.assistant("", message_id="assistant-second", tool_calls=(second_call,)),
        Message.tool_result(second_call, "c", message_id="tool-second"),
        Message.user("continue", message_id="user-continue"),
    )
    policy = RecentToolGroupsContextPolicy(max_tool_groups=1)

    view = policy.select(
        ContextInput(
            agent_id="executor",
            run_id="run-1",
            model="model",
            system_context=SystemContext("Execute one step."),
            transcript=transcript,
            tools=(),
            response_schema=None,
            turn=3,
            budget=ContextBudget(),
            memory=MemorySnapshot.disabled(),
        )
    )

    assert policy.name == "recent_tool_groups_v1:max=1"
    assert [message.message_id for message in view.messages] == [
        "user-start",
        "assistant-second",
        "tool-second",
        "user-continue",
    ]
    assert view.trace.source_message_count == 7
    assert view.trace.selected_message_count == 4
    assert view.trace.omitted_message_count == 3
    assert view.trace.omitted_tool_result_count == 2
    assert view.trace.omitted_message_ids == (
        "assistant-first",
        "tool-first-a",
        "tool-first-b",
    )


def test_plan_react_context_configuration_is_frozen_into_both_snapshots() -> None:
    context = build_plan_react_context(
        Settings(
            plan_react_planner_context_window_tokens=32_768,
            plan_react_planner_context_reserved_output_tokens=4_096,
            plan_react_planner_context_safety_margin_tokens=512,
            plan_react_executor_context_window_tokens=65_536,
            plan_react_executor_context_reserved_output_tokens=8_192,
            plan_react_executor_context_safety_margin_tokens=1_024,
            plan_react_executor_context_max_tool_groups=2,
        )
    )
    flow = build_plan_react_flow(
        artifact_store=InMemoryArtifactStore(),
        planner_context_policy=context.planner_policy,
        planner_context_budget=context.planner_budget,
        executor_context_policy=context.executor_policy,
        executor_context_budget=context.executor_budget,
    )

    assert flow.planner.snapshot.context_policy == "keep_all"
    assert flow.planner.snapshot.context_window_tokens == 32_768
    assert flow.planner.snapshot.context_reserved_output_tokens == 4_096
    assert flow.planner.snapshot.context_safety_margin_tokens == 512
    assert flow.executor.snapshot.context_policy == "recent_tool_groups_v1:max=2"
    assert flow.executor.snapshot.context_window_tokens == 65_536
    assert flow.executor.snapshot.context_reserved_output_tokens == 8_192
    assert flow.executor.snapshot.context_safety_margin_tokens == 1_024


async def test_executor_policy_prunes_old_tool_groups_from_model_requests_and_events() -> None:
    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id=f"sum-{index}",
                        name="calculate_sum",
                        arguments={"left": index, "right": 1},
                    ),
                )
            )
            for index in range(1, 4)
        ]
        + [ModelResponse(content="done")]
    )
    agent = build_agent(
        model,
        definition=replace(STARTER_AGENT_DEFINITION, max_model_turns=4),
        context_policy=RecentToolGroupsContextPolicy(max_tool_groups=1),
    )

    result = await agent.run("calculate three values")

    assert result.status is RunStatus.SUCCEEDED
    assert result.snapshot.context_policy == "recent_tool_groups_v1:max=1"
    assert [len(request.messages) for request in model.requests] == [1, 3, 3, 3]
    assert [
        request.messages[1].tool_calls[0].id for request in model.requests[1:]
    ] == ["sum-1", "sum-2", "sum-3"]
    prepared = [event for event in result.events if event.kind == "context.prepared"]
    assert [event.payload["source_message_count"] for event in prepared] == [1, 3, 5, 7]
    assert [event.payload["selected_message_count"] for event in prepared] == [1, 3, 3, 3]
    assert [event.payload["omitted_message_count"] for event in prepared] == [0, 0, 2, 4]
    assert [event.payload["omitted_tool_result_count"] for event in prepared] == [0, 0, 1, 2]
