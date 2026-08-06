"""Focused harnesses that exercise the same paths as production Agent runs."""

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict

from base_agent.agent import Agent
from base_agent.models import (
    AgentResult,
    Attachment,
    EventType,
    ExecutionPlan,
    ModelRequest,
    Run,
    RuntimeEvent,
    ToolCall,
    ToolConfirmation,
    ToolResult,
)
from base_agent.profiles import AgentProfile
from base_agent.skills import SkillRegistry, select_and_validate_skills
from base_agent.skills.errors import (
    InvalidSkillError,
    SkillNotEnabledError,
    SkillNotFoundError,
    SkillRequirementsError,
)
from base_agent.testing.fake_model import FakeModel
from base_agent.tools import (
    BoundedToolResultPolicy,
    Tool,
    ToolExecutor,
    ToolRegistry,
    ToolResultLimits,
)
from base_agent.tools.registry import ToolNotFoundError


class AgentTestRun(BaseModel):
    """Immutable evidence captured from one Agent Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: AgentResult
    run: Run
    events: tuple[RuntimeEvent, ...]
    model_requests: tuple[ModelRequest, ...]

    @property
    def event_types(self) -> tuple[EventType, ...]:
        """Return the ordered event types for concise lifecycle assertions."""
        return tuple(event.type for event in self.events)


class AgentTestHarness:
    """Run a complete Agent through real Runtime paths with a deterministic model."""

    def __init__(self, agent: Agent) -> None:
        if not isinstance(agent.model, FakeModel):
            raise TypeError("AgentTestHarness requires an Agent using FakeModel")
        self.agent = agent
        self.model = agent.model
        self._request_offsets: dict[UUID, int] = {}

    async def run(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        conversation_id: UUID | None = None,
        skills: Iterable[str] = (),
        plan: ExecutionPlan | None = None,
        planning: bool = False,
        attachments: Iterable[Attachment] = (),
    ) -> AgentTestRun:
        """Execute and capture one Run without bypassing the public Agent facade."""
        active_run_id = run_id or uuid4()
        if active_run_id in self._request_offsets:
            raise ValueError(f"run '{active_run_id}' is already tracked by this harness")
        self._request_offsets[active_run_id] = len(self.model.requests)
        result = await self.agent.run(
            prompt,
            run_id=active_run_id,
            conversation_id=conversation_id,
            skills=skills,
            plan=plan,
            planning=planning,
            attachments=attachments,
        )
        return await self._capture(active_run_id, result)

    async def resume(self, run_id: UUID, user_input: str) -> AgentTestRun:
        """Resume a tracked WAITING Run and return its cumulative evidence."""
        if run_id not in self._request_offsets:
            raise ValueError(f"run '{run_id}' is not tracked by this harness")
        result = await self.agent.resume(run_id, user_input)
        return await self._capture(run_id, result)

    async def confirm(
        self,
        run_id: UUID,
        confirmation: ToolConfirmation,
    ) -> AgentTestRun:
        """Confirm a tracked WAITING Tool request and capture its evidence."""
        if run_id not in self._request_offsets:
            raise ValueError(f"run '{run_id}' is not tracked by this harness")
        result = await self.agent.confirm(run_id, confirmation)
        return await self._capture(run_id, result)

    async def _capture(self, run_id: UUID, result: AgentResult) -> AgentTestRun:
        offset = self._request_offsets[run_id]
        return AgentTestRun(
            result=result,
            run=await self.agent.get_run(run_id),
            events=await self.agent.events(run_id),
            model_requests=self.model.requests[offset:],
        )


class SkillValidationReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str
    valid: bool
    version: str | None = None
    issues: tuple[str, ...] = ()


class ToolHarness:
    """Execute a Tool through the same validation, permission, and timeout path as Runtime."""

    def __init__(
        self,
        tools: Iterable[Tool],
        *,
        max_result_bytes: int = 262_144,
    ) -> None:
        self.registry = ToolRegistry(tools)
        self.executor = ToolExecutor(
            self.registry,
            result_policy=BoundedToolResultPolicy(
                ToolResultLimits(max_bytes=max_result_bytes)
            ),
        )

    async def run(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        permissions: frozenset[str] = frozenset(),
    ) -> ToolResult:
        return await self.executor.execute(
            ToolCall(
                id=f"harness-{uuid4()}",
                name=name,
                arguments=dict(arguments or {}),
            ),
            granted_permissions=permissions,
            allowed_tools=frozenset(self.registry.names),
        )


class SkillHarness:
    """Load and validate a Skill against the exact AgentProfile and Tool contracts."""

    def __init__(self, skill_registry: SkillRegistry, tools: Iterable[Tool] = ()) -> None:
        self.skill_registry = skill_registry
        self.tool_registry = ToolRegistry(tools)

    def validate(self, name: str, *, profile: AgentProfile) -> SkillValidationReport:
        try:
            selected = select_and_validate_skills(
                (name,),
                profile=profile,
                skill_registry=self.skill_registry,
                tool_registry=self.tool_registry,
            )
        except (
            InvalidSkillError,
            SkillNotEnabledError,
            SkillNotFoundError,
            SkillRequirementsError,
            ToolNotFoundError,
        ) as exc:
            return SkillValidationReport(
                skill_name=name,
                valid=False,
                issues=(str(exc),),
            )

        return SkillValidationReport(
            skill_name=name,
            valid=True,
            version=selected[0].manifest.version,
        )
