"""The small application-facing Agent facade."""

from __future__ import annotations

from collections.abc import Coroutine, Iterable, Sequence
from typing import Any
from uuid import uuid4

from base_agent.definitions import AgentDefinition
from base_agent.events import EventStore, InMemoryEventStore
from base_agent.models import RunResult
from base_agent.observability import Observer
from base_agent.providers import ModelProvider
from base_agent.run_handle import RunHandle
from base_agent.runtime.harness import Harness
from base_agent.skills import Skill, SkillCatalog
from base_agent.skills.execution import build_skill_script_tool
from base_agent.skills.loading import (
    SKILL_VIEW_TOOL_NAME,
    build_skill_view_tool,
)
from base_agent.tools import (
    ConfirmationPolicy,
    Tool,
    Toolkit,
    flatten_tools,
)


class Agent:
    def __init__(
        self,
        definition: AgentDefinition,
        *,
        model: ModelProvider,
        tools: Iterable[Tool | Toolkit] = (),
        skills: Iterable[Skill] | SkillCatalog = (),
        confirmation_policy: ConfirmationPolicy | None = None,
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
    ) -> None:
        direct_tools = flatten_tools(tools)
        registered_catalog = skills if isinstance(skills, SkillCatalog) else SkillCatalog(skills)
        selected_catalog = _select_skills(definition, registered_catalog)
        selected_skills = selected_catalog.skills
        if selected_skills and definition.tool_choice.value == "none":
            raise ValueError("selected Skills require Tool use so the model can call skill_view")
        skill_tools = flatten_tools(
            item for skill in selected_skills for item in skill.contributions.tools
        )
        skill_script_tools = tuple(
            script_tool
            for skill in selected_skills
            if (script_tool := build_skill_script_tool(skill)) is not None
        )
        skill_view_tools = (build_skill_view_tool(selected_catalog),) if selected_skills else ()
        registered_tools = flatten_tools(
            (
                *skill_view_tools,
                *direct_tools,
                *skill_tools,
                *skill_script_tools,
            )
        )
        selected_tool_names = set(definition.tools)
        if selected_skills:
            selected_tool_names.add(SKILL_VIEW_TOOL_NAME)
        for skill in selected_skills:
            selected_tool_names.update(skill.dependencies.required_tools)
            selected_tool_names.update(
                tool.spec.name for tool in flatten_tools(skill.contributions.tools)
            )
            missing_skill_permissions = skill.permissions.required - definition.permissions
            if missing_skill_permissions:
                raise ValueError(
                    f"Skill '{skill.name}' requires missing permissions: "
                    f"{', '.join(sorted(missing_skill_permissions))}"
                )
        selected_tool_names.update(tool.spec.name for tool in skill_script_tools)

        tools_by_name = {tool.spec.name: tool for tool in registered_tools}
        for skill in selected_skills:
            selected_tool_names.update(
                name for name in skill.dependencies.optional_tools if name in tools_by_name
            )
        missing_tools = selected_tool_names - tools_by_name.keys()
        if missing_tools:
            raise ValueError(
                f"Agent '{definition.id}' references unregistered Tools: "
                f"{', '.join(sorted(missing_tools))}"
            )
        if definition.tool_choice.value == "required" and not selected_tool_names:
            raise ValueError("tool_choice='required' requires at least one enabled Tool")

        selected_tools = (
            ()
            if definition.tool_choice.value == "none"
            else tuple(tool for tool in registered_tools if tool.spec.name in selected_tool_names)
        )
        if selected_tools and definition.max_model_turns < 2:
            raise ValueError(
                "an Agent with enabled Tools requires max_model_turns >= 2 "
                "so Tool results can be finalized"
            )
        model_name = definition.model or model.info.default_model
        if model_name is None:
            raise ValueError(
                f"Provider '{model.info.name}' has no default model; set AgentDefinition.model"
            )
        if selected_tools and not model.info.capabilities.tools:
            raise ValueError(f"Provider '{model.info.name}' does not support Tool calls")
        if definition.response_model is not None and not model.info.capabilities.structured_output:
            raise ValueError(f"Provider '{model.info.name}' does not support structured output")
        self.definition = definition
        self.event_store = event_store if event_store is not None else InMemoryEventStore()
        self._harness = Harness(
            definition=definition,
            model=model,
            tools=selected_tools,
            skills=selected_skills,
            skill_catalog=selected_catalog,
            confirmation_policy=confirmation_policy,
            observers=observers,
            event_store=self.event_store,
        )

    async def run(
        self,
        prompt: str,
        *,
        on_event: Observer | None = None,
    ) -> RunResult:
        """Run to completion with an optional per-Run live Event observer."""
        return await self._harness.run(prompt, on_event=on_event)

    async def stream(
        self,
        prompt: str,
        *,
        on_event: Observer,
    ) -> RunResult:
        """Run with Provider streaming and publish live Harness events."""
        return await self._harness.run(
            prompt,
            stream=True,
            on_event=on_event,
        )

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
    ) -> RunHandle:
        """Start a process-local background Run and return immediately."""
        run_id = str(uuid4())

        def run_in_background() -> Coroutine[Any, Any, RunResult]:
            return self._harness.run(
                prompt,
                stream=stream,
                run_id=run_id,
            )

        def cancel_in_background() -> Coroutine[Any, Any, RunResult]:
            return self._harness.cancelled_result(
                run_id,
                reason="run cancellation requested",
            )

        return RunHandle._launch(
            run_id=run_id,
            operation=run_in_background,
            cancel_operation=cancel_in_background,
            event_store=self.event_store,
        )


def _select_skills(
    definition: AgentDefinition,
    registered_catalog: SkillCatalog,
) -> SkillCatalog:
    registered_names = {descriptor.name for descriptor in registered_catalog.descriptors}
    missing_skills = set(definition.skills) - registered_names
    if missing_skills:
        raise ValueError(
            f"Agent '{definition.id}' references unregistered Skills: "
            f"{', '.join(sorted(missing_skills))}"
        )
    return registered_catalog.select(definition.skills)
