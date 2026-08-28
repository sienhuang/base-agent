"""Application-owned ContextPolicy configuration for Planner-ReAct."""

from __future__ import annotations

from dataclasses import dataclass

from base_agent.context import (
    ContextBudget,
    ContextInput,
    ContextPolicy,
    ContextTrace,
    ContextView,
    KeepAllContextPolicy,
)
from base_agent.models import Message

from agent_app.config import Settings


@dataclass(frozen=True, slots=True)
class RecentToolGroupsContextPolicy:
    """Keep every non-Tool message and only the newest complete Tool groups."""

    max_tool_groups: int = 4

    def __post_init__(self) -> None:
        if self.max_tool_groups < 1:
            raise ValueError("max_tool_groups must be at least 1")

    @property
    def name(self) -> str:
        # The parameter is part of the name because ContextPolicy is frozen into
        # the Agent snapshot used for resume and independent Worker compatibility.
        return f"recent_tool_groups_v1:max={self.max_tool_groups}"

    def select(self, context: ContextInput, /) -> ContextView:
        groups = _message_groups(context.transcript)
        tool_group_indexes = tuple(
            index for index, group in enumerate(groups) if group[0].tool_calls
        )
        retained_tool_groups = set(tool_group_indexes[-self.max_tool_groups :])
        selected_group_indexes = {
            index
            for index, group in enumerate(groups)
            if not group[0].tool_calls or index in retained_tool_groups
        }
        selected = tuple(
            message
            for index, group in enumerate(groups)
            if index in selected_group_indexes
            for message in group
        )
        omitted = tuple(
            message
            for index, group in enumerate(groups)
            if index not in selected_group_indexes
            for message in group
        )
        fingerprint = context.system_prompt_fingerprint
        return ContextView(
            system_prompt=context.system_prompt,
            messages=selected,
            trace=ContextTrace(
                policy=self.name,
                source_system_prompt_fingerprint=fingerprint,
                selected_system_prompt_fingerprint=fingerprint,
                selected_system_block_ids=context.system_context.block_ids,
                source_message_count=len(context.transcript),
                selected_message_count=len(selected),
                omitted_message_count=len(omitted),
                omitted_tool_result_count=sum(
                    message.role == "tool" for message in omitted
                ),
                selected_message_ids=tuple(
                    message.message_id
                    for message in selected
                    if message.message_id is not None
                ),
                omitted_message_ids=tuple(
                    message.message_id
                    for message in omitted
                    if message.message_id is not None
                ),
                memory_candidate_count=len(context.memory.records),
            ),
        )


@dataclass(frozen=True, slots=True)
class PlanReactContextConfiguration:
    planner_policy: ContextPolicy
    planner_budget: ContextBudget
    executor_policy: ContextPolicy
    executor_budget: ContextBudget


def build_plan_react_context(
    settings: Settings,
) -> PlanReactContextConfiguration:
    """Build one deterministic Planner/Executor context configuration."""
    return PlanReactContextConfiguration(
        planner_policy=KeepAllContextPolicy(),
        planner_budget=ContextBudget(
            context_window_tokens=(
                settings.plan_react_planner_context_window_tokens
            ),
            reserved_output_tokens=(
                settings.plan_react_planner_context_reserved_output_tokens
            ),
            safety_margin_tokens=(
                settings.plan_react_planner_context_safety_margin_tokens
            ),
        ),
        executor_policy=RecentToolGroupsContextPolicy(
            max_tool_groups=(
                settings.plan_react_executor_context_max_tool_groups
            )
        ),
        executor_budget=ContextBudget(
            context_window_tokens=(
                settings.plan_react_executor_context_window_tokens
            ),
            reserved_output_tokens=(
                settings.plan_react_executor_context_reserved_output_tokens
            ),
            safety_margin_tokens=(
                settings.plan_react_executor_context_safety_margin_tokens
            ),
        ),
    )


def _message_groups(transcript: tuple[Message, ...]) -> tuple[tuple[Message, ...], ...]:
    """Partition a transcript without splitting ToolCall/ToolResult groups."""
    groups: list[tuple[Message, ...]] = []
    index = 0
    while index < len(transcript):
        message = transcript[index]
        if message.role == "tool":
            raise ValueError("transcript contains an orphan Tool result")
        if message.role != "assistant" or not message.tool_calls:
            groups.append((message,))
            index += 1
            continue

        pending_call_ids = {call.id for call in message.tool_calls}
        if len(pending_call_ids) != len(message.tool_calls):
            raise ValueError("assistant message contains duplicate ToolCall ids")
        group = [message]
        index += 1
        while pending_call_ids:
            if index >= len(transcript):
                raise ValueError("assistant ToolCall group is missing Tool results")
            result = transcript[index]
            if result.role != "tool" or result.tool_call_id not in pending_call_ids:
                raise ValueError("assistant ToolCall group has non-contiguous Tool results")
            group.append(result)
            pending_call_ids.remove(result.tool_call_id)
            index += 1
        groups.append(tuple(group))
    return tuple(groups)
