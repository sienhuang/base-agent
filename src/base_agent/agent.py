"""Small public facade for starting an agent run."""

import asyncio
from collections.abc import Iterable
from uuid import UUID, uuid4

from base_agent.models import AgentResult, Run, RunStatus, RuntimeEvent
from base_agent.profiles import AgentProfile
from base_agent.providers import ModelProvider
from base_agent.run_handle import RunHandle, request_cancellation
from base_agent.runtime import AgentRuntime
from base_agent.skills import (
    Skill,
    SkillRegistry,
    select_and_validate_skills,
)
from base_agent.stores import (
    CheckpointStore,
    EventStore,
    InMemoryCheckpointStore,
    InMemoryEventStore,
    InMemoryRunStore,
    RunStore,
)
from base_agent.stores.errors import RunNotFoundError
from base_agent.supervision import Supervisor, build_default_supervisor
from base_agent.tools import Tool, ToolExecutor, ToolRegistry


class Agent:
    """Compose a profile, model provider, and runtime without subclassing."""

    def __init__(
        self,
        *,
        profile: AgentProfile,
        model: ModelProvider,
        tools: Iterable[Tool] = (),
        runtime: AgentRuntime | None = None,
        run_store: RunStore | None = None,
        event_store: EventStore | None = None,
        checkpoint_store: CheckpointStore | None = None,
        skill_registry: SkillRegistry | None = None,
        supervisor: Supervisor | None = None,
    ) -> None:
        self.profile = profile
        self.model = model
        self.runtime = runtime or AgentRuntime()
        self.tool_registry = ToolRegistry(tools)
        self.tool_registry.require(profile.tools)
        self.tool_executor = ToolExecutor(self.tool_registry)
        self.run_store = run_store or InMemoryRunStore()
        self.event_store = event_store or InMemoryEventStore()
        self.checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        self.skill_registry = skill_registry or SkillRegistry()
        for skill_name in profile.skills:
            self.skill_registry.manifest(skill_name)
        self.supervisor = supervisor or build_default_supervisor(profile)

    async def run(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        skills: Iterable[str] = (),
    ) -> AgentResult:
        selected_skills = self._select_skills(tuple(skills))
        enabled_tool_names = self._enabled_tools(selected_skills)
        context = self.runtime.create_context(
            self.profile,
            prompt,
            run_id=run_id,
            skills=selected_skills,
            enabled_tool_names=enabled_tool_names,
        )
        return await self.runtime.execute(
            context,
            self.model,
            tool_registry=self.tool_registry,
            tool_executor=self.tool_executor,
            run_store=self.run_store,
            event_store=self.event_store,
            checkpoint_store=self.checkpoint_store,
            supervisor=self.supervisor,
        )

    async def resume(self, run_id: UUID, user_input: str) -> AgentResult:
        """Complete a pending input Tool call and continue the same Run."""
        if not user_input.strip():
            raise ValueError("resume input must not be empty")
        run = await self.run_store.get(run_id)
        if run.status is not RunStatus.WAITING:
            raise ValueError(f"run '{run_id}' is not waiting for input")
        checkpoint = await self.checkpoint_store.load(run_id)
        if checkpoint.profile.id != self.profile.id:
            raise ValueError(
                f"run '{run_id}' belongs to profile '{checkpoint.profile.id}', "
                f"not '{self.profile.id}'"
            )
        self.tool_registry.require(checkpoint.enabled_tool_names)
        checkpoint = await self.checkpoint_store.claim(run_id)
        context = checkpoint.restore()
        try:
            return await self.runtime.execute(
                context,
                self.model,
                tool_registry=self.tool_registry,
                tool_executor=self.tool_executor,
                run_store=self.run_store,
                event_store=self.event_store,
                checkpoint_store=self.checkpoint_store,
                supervisor=self.supervisor,
                resume_input=user_input,
            )
        except Exception:
            await self.checkpoint_store.save(checkpoint)
            raise

    async def start(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        skills: Iterable[str] = (),
    ) -> RunHandle:
        """Start a Run in the current event loop and return after its record is created."""
        active_run_id = run_id or uuid4()
        task = asyncio.create_task(
            self.run(prompt, run_id=active_run_id, skills=skills),
            name=f"base-agent-run-{active_run_id}",
        )
        while True:
            try:
                await self.run_store.get(active_run_id)
                break
            except RunNotFoundError:
                if task.done():
                    await task
                await asyncio.sleep(0)
        return RunHandle(
            run_id=active_run_id,
            _task=task,
            _run_store=self.run_store,
            _event_store=self.event_store,
            _checkpoint_store=self.checkpoint_store,
        )

    async def cancel(self, run_id: UUID) -> Run:
        """Request cooperative cancellation of an active Run."""
        return await request_cancellation(
            run_id,
            run_store=self.run_store,
            event_store=self.event_store,
            checkpoint_store=self.checkpoint_store,
        )

    async def get_run(self, run_id: UUID) -> Run:
        return await self.run_store.get(run_id)

    async def events(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        return await self.event_store.list(run_id)

    def _select_skills(self, names: tuple[str, ...]) -> tuple[Skill, ...]:
        return select_and_validate_skills(
            names,
            profile=self.profile,
            skill_registry=self.skill_registry,
            tool_registry=self.tool_registry,
        )

    def _enabled_tools(self, skills: tuple[Skill, ...]) -> tuple[str, ...]:
        if not skills:
            return self.profile.tools
        allowed = {
            tool_name
            for selected_skill in skills
            for tool_name in selected_skill.manifest.allowed_tools
        }
        return tuple(name for name in self.profile.tools if name in allowed)
