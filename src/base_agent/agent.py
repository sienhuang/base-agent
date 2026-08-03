"""Small public facade for starting an agent run."""

import asyncio
import logging
from collections.abc import Iterable, Mapping
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

from base_agent._logging import reset_log_context, set_log_context
from base_agent.memory import MemoryRetriever
from base_agent.models import (
    AgentResult,
    Artifact,
    Attachment,
    Conversation,
    ConversationTurn,
    ExecutionPlan,
    MemoryFailureMode,
    Message,
    Run,
    RunStatus,
    RuntimeEvent,
)
from base_agent.profiles import AgentProfile
from base_agent.providers import ModelProvider
from base_agent.resources import ResourceSpec
from base_agent.run_handle import (
    RunHandle,
    finalize_task_interruption,
    request_cancellation,
)
from base_agent.runtime import AgentRuntime
from base_agent.skills import (
    Skill,
    SkillRegistry,
    select_and_validate_skills,
)
from base_agent.stores import (
    ArtifactStore,
    CheckpointStore,
    ConversationStore,
    EventStore,
    InMemoryArtifactStore,
    InMemoryCheckpointStore,
    InMemoryConversationStore,
    InMemoryEventStore,
    InMemoryRunStore,
    RunStore,
)
from base_agent.stores.errors import RunNotFoundError
from base_agent.supervision import Supervisor, build_default_supervisor
from base_agent.tools import Tool, ToolExecutor, ToolRegistry

logger = logging.getLogger(__name__)


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
        conversation_store: ConversationStore | None = None,
        skill_registry: SkillRegistry | None = None,
        supervisor: Supervisor | None = None,
        resources: Iterable[ResourceSpec] = (),
        artifact_store: ArtifactStore | None = None,
        memory_retriever: MemoryRetriever | None = None,
        memory_limit: int = 5,
        memory_namespace: str | None = None,
        memory_failure_mode: MemoryFailureMode = MemoryFailureMode.BEST_EFFORT,
        conversation_history_limit: int = 40,
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
        self.conversation_store = conversation_store or InMemoryConversationStore()
        self.skill_registry = skill_registry or SkillRegistry()
        for skill_name in profile.skills:
            self.skill_registry.manifest(skill_name)
        self.supervisor = supervisor or build_default_supervisor(profile)
        self.resources = tuple(resources)
        self.artifact_store = artifact_store or InMemoryArtifactStore()
        if memory_limit < 1 or memory_limit > 100:
            raise ValueError("memory_limit must be between 1 and 100")
        if memory_namespace is not None and not memory_namespace.strip():
            raise ValueError("memory_namespace must not be blank")
        self.memory_retriever = memory_retriever
        self.memory_limit = memory_limit
        self.memory_namespace = memory_namespace
        self.memory_failure_mode = memory_failure_mode
        if conversation_history_limit < 2 or conversation_history_limit % 2:
            raise ValueError(
                "conversation_history_limit must be an even integer of at least 2"
            )
        self.conversation_history_limit = conversation_history_limit
        logger.info(
            "agent initialized",
            extra={
                "event": "agent.initialized",
                "profile_id": profile.id,
                "model_provider": model.name,
                "tool_count": len(self.tool_registry),
                "resource_count": len(self.resources),
            },
        )

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
    ) -> AgentResult:
        active_run_id = run_id or uuid4()
        selected_skill_names = tuple(skills)
        selected_attachment_refs = tuple(attachments)
        log_tokens = set_log_context(
            run_id=active_run_id,
            conversation_id=conversation_id,
            turn_sequence=None,
        )
        started_at = monotonic()
        logger.info(
            "run requested",
            extra={
                "event": "run.requested",
                "profile_id": self.profile.id,
                "skill_count": len(selected_skill_names),
                "attachment_count": len(selected_attachment_refs),
                "planning_requested": planning,
            },
        )
        turn: ConversationTurn | None = None
        history: tuple[Message, ...] = ()
        try:
            selected_skills = self._select_skills(selected_skill_names)
            enabled_tool_names = self._enabled_tools(selected_skills)
            selected_attachments = await self._resolve_attachments(
                selected_attachment_refs
            )
            if conversation_id is not None:
                turn, history = await self.conversation_store.begin_turn(
                    conversation_id,
                    run_id=active_run_id,
                    profile_id=self.profile.id,
                    user_message=prompt,
                )
                history = history[-self.conversation_history_limit :]
                reset_log_context(log_tokens)
                log_tokens = set_log_context(
                    run_id=active_run_id,
                    conversation_id=conversation_id,
                    turn_sequence=turn.sequence,
                )
                logger.info(
                    "conversation turn started",
                    extra={
                        "event": "conversation.turn.started",
                        "history_message_count": len(history),
                    },
                )
            context = self.runtime.create_context(
                self.profile,
                prompt,
                run_id=active_run_id,
                conversation_id=conversation_id,
                turn_sequence=turn.sequence if turn is not None else None,
                conversation_history=history,
                skills=selected_skills,
                enabled_tool_names=enabled_tool_names,
                plan=plan,
                planning_requested=planning,
                attachments=selected_attachments,
            )
            result = await self.runtime.execute(
                context,
                self.model,
                tool_registry=self.tool_registry,
                tool_executor=self.tool_executor,
                run_store=self.run_store,
                event_store=self.event_store,
                checkpoint_store=self.checkpoint_store,
                conversation_store=self.conversation_store,
                supervisor=self.supervisor,
                resource_specs=self.resources,
                artifact_store=self.artifact_store,
                memory_retriever=self.memory_retriever,
                memory_limit=self.memory_limit,
                memory_namespace=self.memory_namespace,
                memory_failure_mode=self.memory_failure_mode,
            )
        except BaseException as exc:
            interrupted_run: Run | None = None
            if isinstance(exc, asyncio.CancelledError):
                interrupted_run = await self._settle_task_interruption(
                    active_run_id, exc
                )
            if (
                conversation_id is not None
                and turn is not None
                and interrupted_run is None
            ):
                try:
                    await self.conversation_store.finish_turn(
                        conversation_id,
                        run_id=active_run_id,
                        status=(
                            RunStatus.INTERRUPTED
                            if isinstance(exc, asyncio.CancelledError)
                            else RunStatus.FAILED
                        ),
                    )
                except Exception as finish_error:
                    exc.add_note(f"failed to release Conversation Turn: {finish_error}")
            log_method = (
                logger.warning
                if isinstance(exc, asyncio.CancelledError)
                else logger.exception
            )
            log_method(
                "run execution raised",
                extra={
                    "event": "run.execution_raised",
                    "profile_id": self.profile.id,
                    "duration_ms": round((monotonic() - started_at) * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
            reset_log_context(log_tokens)
            raise
        logger.info(
            "run finished",
            extra={
                "event": "run.finished",
                "profile_id": self.profile.id,
                "status": result.status.value,
                "duration_ms": round((monotonic() - started_at) * 1000, 3),
                "step_count": result.metadata["steps"],
                "tool_call_count": result.metadata["tool_calls"],
                "model_call_count": result.metadata["model_calls"],
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        )
        reset_log_context(log_tokens)
        return result

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
        log_tokens = set_log_context(
            run_id=run_id,
            conversation_id=run.conversation_id,
            turn_sequence=run.turn_sequence,
        )
        started_at = monotonic()
        logger.info(
            "run resume requested",
            extra={"event": "run.resume_requested", "profile_id": self.profile.id},
        )
        try:
            checkpoint = await self.checkpoint_store.claim(run_id)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                await self._settle_task_interruption(run_id, exc)
            logger.exception(
                "run resume claim failed",
                extra={
                    "event": "run.resume_claim_failed",
                    "duration_ms": round((monotonic() - started_at) * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
            reset_log_context(log_tokens)
            raise
        context = checkpoint.restore()
        try:
            result = await self.runtime.execute(
                context,
                self.model,
                tool_registry=self.tool_registry,
                tool_executor=self.tool_executor,
                run_store=self.run_store,
                event_store=self.event_store,
                checkpoint_store=self.checkpoint_store,
                conversation_store=self.conversation_store,
                supervisor=self.supervisor,
                resource_specs=self.resources,
                artifact_store=self.artifact_store,
                memory_retriever=self.memory_retriever,
                memory_limit=self.memory_limit,
                memory_namespace=self.memory_namespace,
                memory_failure_mode=self.memory_failure_mode,
                resume_input=user_input,
            )
        except Exception as exc:
            try:
                await self.checkpoint_store.save(checkpoint)
            finally:
                logger.exception(
                    "run resume raised",
                    extra={
                        "event": "run.resume_raised",
                        "duration_ms": round((monotonic() - started_at) * 1000, 3),
                        "error_type": type(exc).__name__,
                    },
                )
                reset_log_context(log_tokens)
            raise
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                await self._settle_task_interruption(run_id, exc)
            logger.warning(
                "run resume interrupted",
                extra={
                    "event": "run.resume_interrupted",
                    "duration_ms": round((monotonic() - started_at) * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
            reset_log_context(log_tokens)
            raise
        logger.info(
            "run resume finished",
            extra={
                "event": "run.resume_finished",
                "status": result.status.value,
                "duration_ms": round((monotonic() - started_at) * 1000, 3),
                "model_call_count": result.metadata["model_calls"],
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "total_tokens": result.usage.total_tokens,
            },
        )
        reset_log_context(log_tokens)
        return result

    async def start(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        conversation_id: UUID | None = None,
        skills: Iterable[str] = (),
        plan: ExecutionPlan | None = None,
        planning: bool = False,
        attachments: Iterable[Attachment] = (),
    ) -> RunHandle:
        """Start a Run in the current event loop and return after its record is created."""
        active_run_id = run_id or uuid4()
        task = asyncio.create_task(
            self.run(
                prompt,
                run_id=active_run_id,
                conversation_id=conversation_id,
                skills=skills,
                plan=plan,
                planning=planning,
                attachments=tuple(attachments),
            ),
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
            _conversation_store=self.conversation_store,
        )

    async def cancel(self, run_id: UUID) -> Run:
        """Request cooperative cancellation of an active Run."""
        return await request_cancellation(
            run_id,
            run_store=self.run_store,
            event_store=self.event_store,
            checkpoint_store=self.checkpoint_store,
            conversation_store=self.conversation_store,
        )

    async def create_conversation(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> Conversation:
        conversation = Conversation(
            profile_id=self.profile.id,
            metadata=dict(metadata or {}),
        )
        await self.conversation_store.create_conversation(conversation)
        log_tokens = set_log_context(
            conversation_id=conversation.id,
            run_id=None,
            turn_sequence=None,
        )
        logger.info(
            "conversation created",
            extra={
                "event": "conversation.created",
                "profile_id": self.profile.id,
                "metadata_key_count": len(conversation.metadata),
            },
        )
        reset_log_context(log_tokens)
        return conversation

    async def get_conversation(self, conversation_id: UUID) -> Conversation:
        return await self.conversation_store.get_conversation(conversation_id)

    async def conversation_turns(
        self, conversation_id: UUID
    ) -> tuple[ConversationTurn, ...]:
        return await self.conversation_store.list_turns(conversation_id)

    async def conversation_messages(self, conversation_id: UUID) -> tuple[Message, ...]:
        return await self.conversation_store.messages(conversation_id)

    async def get_run(self, run_id: UUID) -> Run:
        return await self.run_store.get(run_id)

    async def events(self, run_id: UUID) -> tuple[RuntimeEvent, ...]:
        return await self.event_store.list(run_id)

    async def add_attachment(
        self,
        *,
        name: str,
        media_type: str,
        content: bytes,
        metadata: Mapping[str, Any] | None = None,
    ) -> Attachment:
        return await self.artifact_store.add_attachment(
            name=name,
            media_type=media_type,
            content=content,
            metadata=metadata,
        )

    async def get_artifact(self, artifact_id: UUID) -> Artifact:
        return await self.artifact_store.get_artifact(artifact_id)

    async def read_content(self, content_id: UUID) -> bytes:
        return await self.artifact_store.read(content_id)

    async def list_artifacts(self, run_id: UUID) -> tuple[Artifact, ...]:
        return await self.artifact_store.list_artifacts(run_id)

    async def _settle_task_interruption(
        self,
        run_id: UUID,
        interruption: asyncio.CancelledError,
    ) -> Run | None:
        try:
            return await asyncio.shield(
                finalize_task_interruption(
                    run_id,
                    run_store=self.run_store,
                    event_store=self.event_store,
                    checkpoint_store=self.checkpoint_store,
                    conversation_store=self.conversation_store,
                )
            )
        except Exception as interruption_error:
            interruption.add_note(
                f"failed to finalize interrupted Run: {interruption_error}"
            )
            return None

    async def _resolve_attachments(
        self, attachments: tuple[Attachment, ...]
    ) -> tuple[Attachment, ...]:
        if len({attachment.id for attachment in attachments}) != len(attachments):
            raise ValueError("attachments must be unique")
        resolved = []
        for attachment in attachments:
            stored = await self.artifact_store.get_attachment(attachment.id)
            if stored != attachment:
                raise ValueError(
                    f"attachment '{attachment.id}' does not match its stored reference"
                )
            resolved.append(stored)
        return tuple(resolved)

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
