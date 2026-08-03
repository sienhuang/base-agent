"""Lifecycle runtime for provider-neutral agent executions."""

from __future__ import annotations

import asyncio
import logging
from time import monotonic
from uuid import UUID

from base_agent.artifacts import ArtifactManager
from base_agent.memory import MemoryManager, MemoryRetriever
from base_agent.models import (
    AgentResult,
    AgentResultStatus,
    Attachment,
    EventType,
    ExecutionPlan,
    MemoryFailureMode,
    Message,
    MessageRole,
    Run,
    RunStatus,
    ToolResult,
    ToolResultStatus,
)
from base_agent.orchestration import (
    ModelToolStrategy,
    OrchestrationStrategy,
    PlanningStrategy,
    RuntimeServices,
)
from base_agent.orchestration.supervision import apply_supervision_decision
from base_agent.profiles import AgentProfile
from base_agent.providers import ModelProvider
from base_agent.resources import ResourceManager, ResourceSpec
from base_agent.runtime.checkpoint import RuntimeCheckpoint
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.persistence import save_context_snapshot
from base_agent.runtime.state_machine import ExecutionState, InvalidStateTransitionError
from base_agent.skills import Skill
from base_agent.stores import (
    ArtifactStore,
    CheckpointStore,
    ConversationStore,
    EventStore,
    InMemoryArtifactStore,
    InMemoryCheckpointStore,
    InMemoryEventStore,
    InMemoryRunStore,
    RunStore,
)
from base_agent.supervision import SupervisionAction, Supervisor, build_default_supervisor
from base_agent.tools import ToolExecutor, ToolRegistry

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Own run lifecycle while delegating each bounded turn to a strategy."""

    def __init__(
        self,
        strategy: OrchestrationStrategy | None = None,
        *,
        planning_strategy: OrchestrationStrategy | None = None,
    ) -> None:
        self.strategy = strategy or ModelToolStrategy()
        self.planning_strategy = (
            planning_strategy
            if planning_strategy is not None
            else (PlanningStrategy() if strategy is None else None)
        )

    def create_context(
        self,
        profile: AgentProfile,
        prompt: str,
        *,
        run_id: UUID | None = None,
        conversation_id: UUID | None = None,
        turn_sequence: int | None = None,
        conversation_history: tuple[Message, ...] = (),
        skills: tuple[Skill, ...] = (),
        enabled_tool_names: tuple[str, ...] | None = None,
        plan: ExecutionPlan | None = None,
        planning_requested: bool = False,
        attachments: tuple[Attachment, ...] = (),
    ) -> RuntimeContext:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if len({attachment.id for attachment in attachments}) != len(attachments):
            raise ValueError("attachments must be unique")
        if any(
            message.role not in {MessageRole.USER, MessageRole.ASSISTANT}
            for message in conversation_history
        ):
            raise ValueError(
                "conversation history may contain only user and assistant messages"
            )
        if (conversation_id is None) != (turn_sequence is None):
            raise ValueError(
                "conversation_id and turn_sequence must be provided together"
            )
        if plan is not None and planning_requested:
            raise ValueError("plan and planning_requested cannot be used together")
        context = RuntimeContext(
            profile=profile,
            messages=[
                Message.system(self._compose_instructions(profile, skills)),
                *conversation_history,
                Message.user(prompt),
            ],
            conversation_id=conversation_id,
            turn_sequence=turn_sequence,
            skills=skills,
            enabled_tool_names=(
                profile.tools if enabled_tool_names is None else enabled_tool_names
            ),
            plan=plan,
            planning_requested=planning_requested,
            attachments=attachments,
            input_text=prompt,
        )
        if run_id is not None:
            context.run_id = run_id
        return context

    @staticmethod
    def _compose_instructions(profile: AgentProfile, skills: tuple[Skill, ...]) -> str:
        sections = [profile.instructions.strip()]
        for selected_skill in skills:
            manifest = selected_skill.manifest
            sections.append(
                "\n".join(
                    [
                        f"## Skill: {manifest.name} ({manifest.version})",
                        manifest.description,
                        selected_skill.instructions,
                    ]
                )
            )
        return "\n\n".join(sections)

    async def execute(
        self,
        context: RuntimeContext,
        provider: ModelProvider,
        *,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        run_store: RunStore | None = None,
        event_store: EventStore | None = None,
        checkpoint_store: CheckpointStore | None = None,
        conversation_store: ConversationStore | None = None,
        supervisor: Supervisor | None = None,
        resume_input: str | None = None,
        resource_specs: tuple[ResourceSpec, ...] = (),
        artifact_store: ArtifactStore | None = None,
        memory_retriever: MemoryRetriever | None = None,
        memory_limit: int = 5,
        memory_namespace: str | None = None,
        memory_failure_mode: MemoryFailureMode = MemoryFailureMode.BEST_EFFORT,
    ) -> AgentResult:
        runtime_started_at = monotonic()
        correlation = {
            "run_id": str(context.run_id),
            "conversation_id": (
                str(context.conversation_id)
                if context.conversation_id is not None
                else None
            ),
            "turn_sequence": context.turn_sequence,
        }
        active_run_store = run_store or InMemoryRunStore()
        active_event_store = event_store or InMemoryEventStore()
        active_checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        active_artifact_store = artifact_store or InMemoryArtifactStore()
        active_supervisor = supervisor or build_default_supervisor(context.profile)
        resources = ResourceManager(
            resource_specs,
            context=context,
            event_store=active_event_store,
        )
        artifacts = ArtifactManager(
            context=context,
            store=active_artifact_store,
            event_store=active_event_store,
        )
        memories = MemoryManager(
            context=context,
            retriever=memory_retriever,
            event_store=active_event_store,
            limit=memory_limit,
            namespace=memory_namespace,
            failure_mode=memory_failure_mode,
        )
        services = RuntimeServices(
            provider=provider,
            tool_registry=tool_registry,
            tool_executor=tool_executor,
            run_store=active_run_store,
            event_store=active_event_store,
            supervisor=active_supervisor,
            resources=resources,
            artifacts=artifacts,
            memories=memories,
        )
        active_strategy = (
            self.planning_strategy
            if (context.plan is not None or context.planning_requested)
            and self.planning_strategy is not None
            else self.strategy
        )
        logger.info(
            "runtime execution started",
            extra={
                "event": "runtime.execution.started",
                **correlation,
                "profile_id": context.profile.id,
                "provider": provider.name,
                "initial_state": context.state.value,
            },
        )
        if context.state_machine.state is ExecutionState.CREATED:
            await self._create_run(context, active_run_store, active_event_store)
            context.state_machine.transition_to(ExecutionState.RUNNING)
            context.provider_name = provider.name
            await save_context_snapshot(context, active_run_store)
            await active_event_store.emit(
                context.run_id, EventType.RUN_STARTED, {"provider": provider.name}
            )
        elif context.state_machine.state is ExecutionState.RUNNING:
            # Backwards-compatible support for caller-created RUNNING contexts.
            await self._create_run(context, active_run_store, active_event_store)
            context.provider_name = provider.name
            await save_context_snapshot(context, active_run_store)
            await active_event_store.emit(
                context.run_id, EventType.RUN_STARTED, {"provider": provider.name}
            )
        elif context.state_machine.state is ExecutionState.WAITING:
            await self._prepare_resume(
                context,
                resume_input,
                provider=provider,
                run_store=active_run_store,
                event_store=active_event_store,
            )
        else:
            raise InvalidStateTransitionError(
                f"cannot execute context in state {context.state_machine.state.value}"
            )

        execution_error: BaseException | None = None
        try:
            await resources.start()
            await memories.initialize()
            while context.state_machine.state is ExecutionState.RUNNING:
                if await active_run_store.is_cancel_requested(context.run_id):
                    context.error = "run cancellation requested"
                    context.state_machine.transition_to(ExecutionState.CANCELLED)
                    break

                model_decision = await active_supervisor.before_model(context)
                if model_decision.action is not SupervisionAction.CONTINUE:
                    await apply_supervision_decision(
                        context, model_decision, active_event_store
                    )
                if context.state_machine.state is not ExecutionState.RUNNING:
                    break

                await active_strategy.advance(context, services)
        except asyncio.CancelledError as exc:
            execution_error = exc
            if await active_run_store.is_cancel_requested(context.run_id):
                context.error = "run cancellation requested"
                if context.state in {
                    ExecutionState.CREATED,
                    ExecutionState.RUNNING,
                    ExecutionState.WAITING,
                }:
                    context.state_machine.transition_to(ExecutionState.CANCELLED)
            else:
                context.error = "runtime task interrupted"
                if context.state in {
                    ExecutionState.CREATED,
                    ExecutionState.RUNNING,
                    ExecutionState.WAITING,
                }:
                    context.state_machine.transition_to(ExecutionState.INTERRUPTED)
            logger.warning(
                "runtime task cancelled",
                extra={
                    "event": "runtime.execution.cancelled",
                    **correlation,
                    "duration_ms": round((monotonic() - runtime_started_at) * 1000, 3),
                },
            )
        except Exception as exc:
            execution_error = exc
            logger.exception(
                "runtime execution failed",
                extra={
                    "event": "runtime.execution.failed",
                    **correlation,
                    "duration_ms": round((monotonic() - runtime_started_at) * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
            context.error = f"orchestration failed: {exc}"
            if context.state is ExecutionState.RUNNING:
                context.state_machine.transition_to(ExecutionState.FAILED)
        finally:
            context.resource_failures.extend(await resources.close(execution_error))

        if isinstance(active_strategy, PlanningStrategy):
            await active_strategy.settle(context, services)
        result = self._build_result(context)
        if context.state is ExecutionState.WAITING:
            await active_checkpoint_store.save(RuntimeCheckpoint.from_context(context))
        else:
            await active_checkpoint_store.delete(context.run_id)
        if context.conversation_id is not None:
            if conversation_store is None:
                raise ValueError(
                    "conversation_store is required for a Conversation Run"
                )
            await conversation_store.finish_turn(
                context.conversation_id,
                run_id=context.run_id,
                status=RunStatus(context.state.value),
                assistant_message=context.output,
            )
        await self._finalize_run(context, active_run_store, active_event_store)
        logger.info(
            "runtime execution finished",
            extra={
                "event": "runtime.execution.finished",
                **correlation,
                "status": result.status.value,
                "duration_ms": round((monotonic() - runtime_started_at) * 1000, 3),
                "step_count": context.step_count,
                "tool_call_count": context.tool_call_count,
                "input_tokens": context.usage.input_tokens,
                "output_tokens": context.usage.output_tokens,
                "total_tokens": context.usage.total_tokens,
                "model_call_count": len(context.responses),
                "resource_failure_count": len(context.resource_failures),
            },
        )
        if isinstance(execution_error, asyncio.CancelledError):
            raise execution_error
        return result

    @staticmethod
    async def _prepare_resume(
        context: RuntimeContext,
        resume_input: str | None,
        *,
        provider: ModelProvider,
        run_store: RunStore,
        event_store: EventStore,
    ) -> None:
        if resume_input is None or not resume_input.strip():
            raise ValueError("resume input must not be empty")
        pending = context.pending_input
        if pending is None:
            raise InvalidStateTransitionError("waiting context has no pending input")
        tool_result = ToolResult(
            tool_name=pending.tool_name,
            status=ToolResultStatus.SUCCESS,
            data={"input": resume_input},
        )
        context.messages.append(
            Message.tool(tool_result.model_dump_json(), tool_call_id=pending.tool_call_id)
        )
        context.pending_input = None
        context.output = None
        context.error = None
        context.provider_name = provider.name
        context.state_machine.transition_to(ExecutionState.RUNNING)
        await save_context_snapshot(context, run_store)
        await event_store.emit(
            context.run_id,
            EventType.INPUT_RECEIVED,
            {
                "tool_call_id": pending.tool_call_id,
                "tool_name": pending.tool_name,
                "input": resume_input,
            },
        )
        await event_store.emit(
            context.run_id,
            EventType.RUN_RESUMED,
            {"provider": provider.name, "from": ExecutionState.WAITING.value},
        )

    @staticmethod
    async def _create_run(
        context: RuntimeContext,
        run_store: RunStore,
        event_store: EventStore,
    ) -> None:
        run = Run(
            id=context.run_id,
            profile_id=context.profile.id,
            conversation_id=context.conversation_id,
            turn_sequence=context.turn_sequence,
            skills=tuple(skill.manifest.reference() for skill in context.skills),
            attachments=context.attachments,
            artifacts=tuple(context.artifacts),
        )
        await run_store.create(run)
        await event_store.emit(
            context.run_id,
            EventType.RUN_CREATED,
            {
                "profile_id": context.profile.id,
                "conversation_id": (
                    str(context.conversation_id)
                    if context.conversation_id is not None
                    else None
                ),
                "turn_sequence": context.turn_sequence,
            },
        )
        for selected_skill in context.skills:
            skill_data = {
                "name": selected_skill.manifest.name,
                "version": selected_skill.manifest.version,
            }
            await event_store.emit(
                context.run_id, EventType.SKILL_SELECTED, skill_data
            )
            await event_store.emit(context.run_id, EventType.SKILL_LOADED, skill_data)
        if context.plan is not None:
            await event_store.emit(
                context.run_id,
                EventType.PLAN_CREATED,
                {"plan": context.plan.model_dump(mode="json")},
            )
        for attachment in context.attachments:
            await event_store.emit(
                context.run_id,
                EventType.ATTACHMENT_ADDED,
                {"attachment": attachment.model_dump(mode="json")},
            )

    @staticmethod
    async def _finalize_run(
        context: RuntimeContext,
        run_store: RunStore,
        event_store: EventStore,
    ) -> None:
        await save_context_snapshot(context, run_store)
        terminal_events = {
            ExecutionState.COMPLETED: EventType.RUN_COMPLETED,
            ExecutionState.FAILED: EventType.RUN_FAILED,
            ExecutionState.CANCELLED: EventType.RUN_CANCELLED,
            ExecutionState.INTERRUPTED: EventType.RUN_INTERRUPTED,
            ExecutionState.LIMIT_REACHED: EventType.RUN_LIMIT_REACHED,
            ExecutionState.WAITING: EventType.RUN_WAITING,
        }
        try:
            event_type = terminal_events[context.state_machine.state]
        except KeyError as exc:
            raise InvalidStateTransitionError(
                f"cannot finalize state {context.state_machine.state.value}"
            ) from exc
        event_data: dict[str, object] = {
            "steps": context.step_count,
            "tool_calls": context.tool_call_count,
            "output": context.output,
            "error": context.error,
            "usage": context.usage.model_dump(mode="json"),
            "model_calls": len(context.responses),
            "pending_input": (
                context.pending_input.model_dump(mode="json")
                if context.pending_input is not None
                else None
            ),
            "plan": (
                context.plan.model_dump(mode="json") if context.plan is not None else None
            ),
            "resource_failures": [
                failure.model_dump(mode="json")
                for failure in context.resource_failures
            ],
            "attachments": [
                attachment.model_dump(mode="json")
                for attachment in context.attachments
            ],
            "artifacts": [
                artifact.model_dump(mode="json") for artifact in context.artifacts
            ],
            "memory": {
                "initialized": context.memory_initialized,
                "error": context.memory_error,
                "matches": [
                    {"id": str(match.record.id), "score": match.score}
                    for match in context.memories
                ],
            },
        }
        if context.state is ExecutionState.INTERRUPTED:
            event_data["interruption_source"] = "asyncio_task_cancelled"
            event_data["recoverable"] = False
        await event_store.emit(context.run_id, event_type, event_data)

    @staticmethod
    def _build_result(context: RuntimeContext) -> AgentResult:
        status_map = {
            ExecutionState.COMPLETED: AgentResultStatus.COMPLETED,
            ExecutionState.FAILED: AgentResultStatus.FAILED,
            ExecutionState.CANCELLED: AgentResultStatus.CANCELLED,
            ExecutionState.INTERRUPTED: AgentResultStatus.INTERRUPTED,
            ExecutionState.LIMIT_REACHED: AgentResultStatus.LIMIT_REACHED,
            ExecutionState.WAITING: AgentResultStatus.WAITING,
        }
        try:
            status = status_map[context.state_machine.state]
        except KeyError as exc:
            raise InvalidStateTransitionError(
                f"cannot build a result from state {context.state_machine.state.value}"
            ) from exc
        return AgentResult(
            status=status,
            output=context.output,
            messages=tuple(context.messages),
            usage=context.usage,
            error=context.error,
            attachments=context.attachments,
            artifacts=tuple(context.artifacts),
            metadata={
                "run_id": str(context.run_id),
                "conversation_id": (
                    str(context.conversation_id)
                    if context.conversation_id is not None
                    else None
                ),
                "turn_sequence": context.turn_sequence,
                "steps": context.step_count,
                "tool_calls": context.tool_call_count,
                "provider": context.provider_name,
                "planning_requested": context.planning_requested,
                **(
                    {"react": context.supervision_data["react_strategy"]}
                    if "react_strategy" in context.supervision_data
                    else {}
                ),
                "model_calls": len(context.responses),
                "pending_input": (
                    context.pending_input.model_dump(mode="json")
                    if context.pending_input is not None
                    else None
                ),
                "plan": (
                    context.plan.model_dump(mode="json") if context.plan is not None else None
                ),
                "resource_failures": [
                    failure.model_dump(mode="json")
                    for failure in context.resource_failures
                ],
                "memory": {
                    "initialized": context.memory_initialized,
                    "error": context.memory_error,
                    "matches": [
                        {"id": str(match.record.id), "score": match.score}
                        for match in context.memories
                    ],
                },
            },
        )
