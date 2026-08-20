"""The small application-facing Agent facade."""

from __future__ import annotations

from collections.abc import Coroutine, Iterable, Sequence
from typing import Any
from uuid import uuid4

from base_agent.artifacts import ArtifactStore, InMemoryArtifactStore
from base_agent.checkpoints import CheckpointStore, InMemoryCheckpointStore
from base_agent.context import (
    ContextBudget,
    ContextPolicy,
    KeepAllContextPolicy,
    SystemContext,
    SystemContextBlock,
)
from base_agent.definitions import AgentDefinition
from base_agent.events import EventStore, InMemoryEventStore
from base_agent.governance import ToolConfirmation
from base_agent.memory import (
    DisabledMemoryProvider,
    DisabledMemoryWriter,
    MemoryProvider,
    MemoryScope,
    MemoryWriter,
)
from base_agent.models import HarnessSnapshot, RunInvocation, RunResult, RunStatus
from base_agent.observability import Observer
from base_agent.providers import ModelProvider
from base_agent.resources import ResourceSpec
from base_agent.run_handle import RunHandle
from base_agent.runtime.harness import Harness
from base_agent.skills import Skill, SkillCatalog, SkillDescriptor
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
        context_policy: ContextPolicy | None = None,
        context_budget: ContextBudget | None = None,
        system_context_blocks: Iterable[SystemContextBlock] = (),
        memory_provider: MemoryProvider | None = None,
        memory_writer: MemoryWriter | None = None,
        memory_recall_limit: int = 5,
        memory_recall_timeout_seconds: float = 2.0,
        observers: Sequence[Observer] = (),
        event_store: EventStore | None = None,
        checkpoint_store: CheckpointStore | None = None,
        checkpoint_lease_seconds: float = 300.0,
        artifact_store: ArtifactStore | None = None,
        resources: Iterable[ResourceSpec] = (),
    ) -> None:
        resolved_context_policy = (
            context_policy if context_policy is not None else KeepAllContextPolicy()
        )
        context_policy_name = resolved_context_policy.name
        if not context_policy_name.strip():
            raise ValueError("context policy name must not be blank")
        if context_policy_name != context_policy_name.strip():
            raise ValueError("context policy name must not have surrounding whitespace")
        resolved_memory_provider = (
            memory_provider if memory_provider is not None else DisabledMemoryProvider()
        )
        memory_provider_name = resolved_memory_provider.name
        if not memory_provider_name.strip():
            raise ValueError("memory provider name must not be blank")
        if memory_provider_name != memory_provider_name.strip():
            raise ValueError("memory provider name must not have surrounding whitespace")
        resolved_memory_writer = (
            memory_writer if memory_writer is not None else DisabledMemoryWriter()
        )
        memory_writer_name = resolved_memory_writer.name
        if not memory_writer_name.strip():
            raise ValueError("memory writer name must not be blank")
        if memory_writer_name != memory_writer_name.strip():
            raise ValueError("memory writer name must not have surrounding whitespace")
        if memory_recall_limit < 1:
            raise ValueError("memory_recall_limit must be at least 1")
        if memory_recall_timeout_seconds <= 0:
            raise ValueError("memory_recall_timeout_seconds must be positive")
        if checkpoint_lease_seconds <= 0:
            raise ValueError("checkpoint_lease_seconds must be positive")
        resource_specs = tuple(resources)
        resource_names = [spec.name for spec in resource_specs]
        if len(set(resource_names)) != len(resource_names):
            raise ValueError("registered resources must have unique names")
        direct_tools = flatten_tools(tools)
        registered_catalog = skills if isinstance(skills, SkillCatalog) else SkillCatalog(skills)
        selected_catalog = _select_skills(definition, registered_catalog)
        selected_skills = selected_catalog.skills
        system_context = SystemContext(
            instructions=definition.instructions,
            blocks=(
                *tuple(system_context_blocks),
                *_skill_system_context_blocks(selected_catalog.descriptors),
            ),
        )
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
        self.checkpoint_store = (
            checkpoint_store if checkpoint_store is not None else InMemoryCheckpointStore()
        )
        self._checkpoint_lease_seconds = checkpoint_lease_seconds
        self.artifact_store = (
            artifact_store if artifact_store is not None else InMemoryArtifactStore()
        )
        self._harness = Harness(
            definition=definition,
            model=model,
            tools=selected_tools,
            skills=selected_skills,
            confirmation_policy=confirmation_policy,
            context_policy=resolved_context_policy,
            context_budget=(context_budget if context_budget is not None else ContextBudget()),
            system_context=system_context,
            memory_provider=resolved_memory_provider,
            memory_writer=resolved_memory_writer,
            memory_recall_limit=memory_recall_limit,
            memory_recall_timeout_seconds=memory_recall_timeout_seconds,
            observers=observers,
            event_store=self.event_store,
            checkpoint_store=self.checkpoint_store,
            checkpoint_lease_seconds=checkpoint_lease_seconds,
            artifact_store=self.artifact_store,
            resources=resource_specs,
        )

    async def run(
        self,
        prompt: str,
        *,
        on_event: Observer | None = None,
        memory_scope: MemoryScope | None = None,
        invocation: RunInvocation | None = None,
    ) -> RunResult:
        """Run to completion with an optional per-Run live Event observer."""
        return await self._harness.run(
            prompt,
            on_event=on_event,
            memory_scope=memory_scope,
            invocation=invocation,
        )

    @property
    def snapshot(self) -> HarnessSnapshot:
        """Stable identity of the Agent configuration used by new Runs."""
        return self._harness.snapshot()

    async def stream(
        self,
        prompt: str,
        *,
        on_event: Observer,
        memory_scope: MemoryScope | None = None,
        invocation: RunInvocation | None = None,
    ) -> RunResult:
        """Run with Provider streaming and publish live Harness events."""
        return await self._harness.run(
            prompt,
            stream=True,
            on_event=on_event,
            memory_scope=memory_scope,
            invocation=invocation,
        )

    async def resume(
        self,
        run_id: str,
        user_input: str,
        *,
        on_event: Observer | None = None,
    ) -> RunResult:
        """Continue a Run waiting on a Tool-produced input request."""
        return await self._harness.resume(run_id, user_input, on_event=on_event)

    async def confirm(
        self,
        run_id: str,
        confirmation: ToolConfirmation,
        *,
        on_event: Observer | None = None,
    ) -> RunResult:
        """Approve or reject one exact pending Tool confirmation."""
        return await self._harness.confirm(run_id, confirmation, on_event=on_event)

    async def cancel(self, run_id: str) -> RunResult:
        """Cancel a checkpointed WAITING Run and consume its continuation."""
        checkpoint = await self.checkpoint_store.load(run_id)
        if checkpoint.agent_id != self.definition.id:
            raise ValueError(
                f"run '{run_id}' belongs to Agent '{checkpoint.agent_id}', "
                f"not '{self.definition.id}'"
            )
        claim = await self.checkpoint_store.claim(
            run_id,
            owner_id=f"cancel:{self.definition.id}:{uuid4()}",
            lease_seconds=self._checkpoint_lease_seconds,
        )
        result = await self._harness.cancelled_result(
            run_id,
            reason="run cancellation requested",
        )
        await self.checkpoint_store.complete(claim)
        return result

    def start(
        self,
        prompt: str,
        *,
        stream: bool = False,
        on_event: Observer | None = None,
        memory_scope: MemoryScope | None = None,
        invocation: RunInvocation | None = None,
        run_id: str | None = None,
    ) -> RunHandle:
        """Start a process-local background Run and return immediately."""
        resolved_run_id = run_id or str(uuid4())
        if not resolved_run_id.strip():
            raise ValueError("run_id must not be blank when provided")

        def run_in_background() -> Coroutine[Any, Any, RunResult]:
            return self._harness.run(
                prompt,
                stream=stream,
                on_event=on_event,
                run_id=resolved_run_id,
                memory_scope=memory_scope,
                invocation=invocation,
            )

        def cancel_in_background() -> Coroutine[Any, Any, RunResult]:
            if handle.status is RunStatus.WAITING:
                return self.cancel(resolved_run_id)
            return self._harness.cancelled_result(
                resolved_run_id,
                reason="run cancellation requested",
            )

        handle = RunHandle._launch(
            run_id=resolved_run_id,
            operation=run_in_background,
            cancel_operation=cancel_in_background,
            event_store=self.event_store,
        )
        return handle


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


def _skill_system_context_blocks(
    skills: Sequence[SkillDescriptor],
) -> tuple[SystemContextBlock, ...]:
    if not skills:
        return ()
    skill_lines = []
    for skill in skills:
        detail = f": {skill.description}" if skill.description else ""
        skill_lines.append(f"- `{skill.name}` (version {skill.version}){detail}")
    return (
        SystemContextBlock(
            id="base_agent.skills.catalog",
            required=True,
            content=(
                "# Available skills\n\n"
                "The Skills below are available for this Run. Before following a "
                "Skill, call `skill_view` with its name to load the complete "
                "instructions. Load supporting files only when the playbook "
                "requires them.\n\n" + "\n".join(skill_lines)
            ),
        ),
    )
