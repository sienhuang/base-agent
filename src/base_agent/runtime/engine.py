"""Text-first implementation of the model execution loop."""

from __future__ import annotations

from uuid import UUID

from base_agent.models import (
    AgentResult,
    AgentResultStatus,
    EventType,
    Message,
    ModelRequest,
    PendingInput,
    Run,
    RunStatus,
    ToolResult,
    ToolResultStatus,
)
from base_agent.models.run import utc_now
from base_agent.profiles import AgentProfile
from base_agent.providers import ModelProvider
from base_agent.runtime.checkpoint import RuntimeCheckpoint
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.state_machine import ExecutionState, InvalidStateTransitionError
from base_agent.skills import Skill
from base_agent.stores import (
    CheckpointStore,
    EventStore,
    InMemoryCheckpointStore,
    InMemoryEventStore,
    InMemoryRunStore,
    RunStore,
)
from base_agent.supervision import (
    SupervisionAction,
    SupervisionDecision,
    Supervisor,
    build_default_supervisor,
)
from base_agent.tools import ToolExecutor, ToolRegistry


class AgentRuntime:
    """Advance a single-agent run using a provider-neutral model interface."""

    def create_context(
        self,
        profile: AgentProfile,
        prompt: str,
        *,
        run_id: UUID | None = None,
        skills: tuple[Skill, ...] = (),
        enabled_tool_names: tuple[str, ...] | None = None,
    ) -> RuntimeContext:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        context = RuntimeContext(
            profile=profile,
            messages=[
                Message.system(self._compose_instructions(profile, skills)),
                Message.user(prompt),
            ],
            skills=skills,
            enabled_tool_names=(
                profile.tools if enabled_tool_names is None else enabled_tool_names
            ),
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
        supervisor: Supervisor | None = None,
        resume_input: str | None = None,
    ) -> AgentResult:
        active_run_store = run_store or InMemoryRunStore()
        active_event_store = event_store or InMemoryEventStore()
        active_checkpoint_store = checkpoint_store or InMemoryCheckpointStore()
        active_supervisor = supervisor or build_default_supervisor(context.profile)

        if context.state_machine.state is ExecutionState.CREATED:
            await self._create_run(context, active_run_store, active_event_store)
            context.state_machine.transition_to(ExecutionState.RUNNING)
            context.provider_name = provider.name
            await self._save_snapshot(context, active_run_store)
            await active_event_store.emit(
                context.run_id,
                EventType.RUN_STARTED,
                {"provider": provider.name},
            )
        elif context.state_machine.state is ExecutionState.RUNNING:
            await self._create_run(context, active_run_store, active_event_store)
            context.provider_name = provider.name
            await self._save_snapshot(context, active_run_store)
            await active_event_store.emit(
                context.run_id,
                EventType.RUN_STARTED,
                {"provider": provider.name},
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

        while context.state_machine.state is ExecutionState.RUNNING:
            if await active_run_store.is_cancel_requested(context.run_id):
                context.error = "run cancellation requested"
                context.state_machine.transition_to(ExecutionState.CANCELLED)
                break

            model_decision = await active_supervisor.before_model(context)
            if model_decision.action is not SupervisionAction.CONTINUE:
                await self._apply_supervision_decision(
                    context,
                    model_decision,
                    active_event_store,
                )
            if context.state_machine.state is not ExecutionState.RUNNING:
                break

            await self._advance(
                context,
                provider,
                tool_registry=tool_registry,
                tool_executor=tool_executor,
                run_store=active_run_store,
                event_store=active_event_store,
                supervisor=active_supervisor,
            )

        result = self._build_result(context)
        if context.state is ExecutionState.WAITING:
            await active_checkpoint_store.save(RuntimeCheckpoint.from_context(context))
        else:
            await active_checkpoint_store.delete(context.run_id)
        await self._finalize_run(context, active_run_store, active_event_store)
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
        await AgentRuntime._save_snapshot(context, run_store)
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

    async def _advance(
        self,
        context: RuntimeContext,
        provider: ModelProvider,
        *,
        tool_registry: ToolRegistry | None,
        tool_executor: ToolExecutor | None,
        run_store: RunStore,
        event_store: EventStore,
        supervisor: Supervisor,
    ) -> None:
        context.step_count += 1
        definitions = (
            tool_registry.definitions(context.enabled_tool_names)
            if tool_registry is not None
            else ()
        )
        request = ModelRequest(
            messages=tuple(context.messages),
            tools=definitions,
            model=context.profile.model,
        )
        await event_store.emit(
            context.run_id,
            EventType.MODEL_REQUESTED,
            {"step": context.step_count, "request": request.model_dump(mode="json")},
        )

        try:
            response = await provider.complete(request)
        except Exception as exc:
            context.error = f"model provider '{provider.name}' failed: {exc}"
            context.state_machine.transition_to(ExecutionState.FAILED)
            return

        await event_store.emit(
            context.run_id,
            EventType.MODEL_RESPONDED,
            {"step": context.step_count, "response": response.model_dump(mode="json")},
        )

        context.responses.append(response)
        context.usage = context.usage + response.usage
        context.messages.append(response.to_assistant_message())
        await self._save_snapshot(context, run_store)

        if await run_store.is_cancel_requested(context.run_id):
            context.error = "run cancellation requested"
            context.state_machine.transition_to(ExecutionState.CANCELLED)
            return

        if response.tool_calls:
            executor = tool_executor or ToolExecutor(tool_registry or ToolRegistry())
            for call in response.tool_calls:
                if await run_store.is_cancel_requested(context.run_id):
                    context.error = "run cancellation requested"
                    context.state_machine.transition_to(ExecutionState.CANCELLED)
                    return
                call_data = {"step": context.step_count, "call": call.model_dump(mode="json")}
                await event_store.emit(
                    context.run_id,
                    EventType.TOOL_REQUESTED,
                    call_data,
                )
                decision = await supervisor.before_tool(context, call)
                if decision.action is not SupervisionAction.CONTINUE:
                    await self._apply_supervision_decision(
                        context,
                        decision,
                        event_store,
                        append_message=False,
                    )
                    if decision.action is SupervisionAction.STOP:
                        return
                    context.tool_call_count += 1
                    blocked = ToolResult(
                        tool_name=call.name,
                        status=ToolResultStatus.DENIED,
                        error_code="supervisor_intervention",
                        message=decision.reason,
                    )
                    context.messages.append(
                        Message.tool(blocked.model_dump_json(), tool_call_id=call.id)
                    )
                    if decision.message:
                        context.messages.append(Message.system(decision.message))
                    await event_store.emit(
                        context.run_id,
                        EventType.TOOL_FAILED,
                        {
                            "step": context.step_count,
                            "call_id": call.id,
                            "result": blocked.model_dump(mode="json"),
                        },
                    )
                    await self._save_snapshot(context, run_store)
                    return

                context.tool_call_count += 1
                await self._save_snapshot(context, run_store)
                await event_store.emit(
                    context.run_id,
                    EventType.TOOL_STARTED,
                    call_data,
                )
                result = await executor.execute(
                    call,
                    granted_permissions=context.profile.permissions,
                    allowed_tools=frozenset(context.enabled_tool_names),
                )
                if result.status is ToolResultStatus.WAITING:
                    wait_data = result.data if isinstance(result.data, dict) else {}
                    prompt = wait_data.get("prompt")
                    if not isinstance(prompt, str) or not prompt.strip():
                        context.error = f"tool '{call.name}' returned an invalid input request"
                        context.state_machine.transition_to(ExecutionState.FAILED)
                        return
                    metadata = wait_data.get("metadata")
                    context.pending_input = PendingInput(
                        tool_call_id=call.id,
                        tool_name=call.name,
                        prompt=prompt,
                        metadata=metadata if isinstance(metadata, dict) else {},
                    )
                    context.state_machine.transition_to(ExecutionState.WAITING)
                    await event_store.emit(
                        context.run_id,
                        EventType.TOOL_WAITING,
                        {
                            "step": context.step_count,
                            "call_id": call.id,
                            "result": result.model_dump(mode="json"),
                        },
                    )
                    await self._save_snapshot(context, run_store)
                    return
                result_event = (
                    EventType.TOOL_COMPLETED
                    if result.status is ToolResultStatus.SUCCESS
                    else EventType.TOOL_FAILED
                )
                await event_store.emit(
                    context.run_id,
                    result_event,
                    {
                        "step": context.step_count,
                        "call_id": call.id,
                        "result": result.model_dump(mode="json"),
                    },
                )
                context.messages.append(
                    Message.tool(result.model_dump_json(), tool_call_id=call.id)
                )
                decision = await supervisor.after_tool(context, call, result)
                if decision.action is not SupervisionAction.CONTINUE:
                    await self._apply_supervision_decision(context, decision, event_store)
                    await self._save_snapshot(context, run_store)
                    return
                if await run_store.is_cancel_requested(context.run_id):
                    context.error = "run cancellation requested"
                    context.state_machine.transition_to(ExecutionState.CANCELLED)
                    return
            return

        context.output = response.content
        context.state_machine.transition_to(ExecutionState.COMPLETED)

    @staticmethod
    async def _create_run(
        context: RuntimeContext,
        run_store: RunStore,
        event_store: EventStore,
    ) -> None:
        run = Run(
            id=context.run_id,
            profile_id=context.profile.id,
            skills=tuple(skill.manifest.reference() for skill in context.skills),
        )
        await run_store.create(run)
        await event_store.emit(
            context.run_id,
            EventType.RUN_CREATED,
            {"profile_id": context.profile.id},
        )
        for selected_skill in context.skills:
            skill_data = {
                "name": selected_skill.manifest.name,
                "version": selected_skill.manifest.version,
            }
            await event_store.emit(
                context.run_id,
                EventType.SKILL_SELECTED,
                skill_data,
            )
            await event_store.emit(
                context.run_id,
                EventType.SKILL_LOADED,
                skill_data,
            )

    @staticmethod
    async def _save_snapshot(context: RuntimeContext, run_store: RunStore) -> None:
        existing = await run_store.get(context.run_id)
        updated = existing.model_copy(
            update={
                "status": RunStatus(context.state_machine.state),
                "step_count": context.step_count,
                "tool_call_count": context.tool_call_count,
                "usage": context.usage,
                "output": context.output,
                "error": context.error,
                "metadata": {
                    **existing.metadata,
                    "pending_input": (
                        context.pending_input.model_dump(mode="json")
                        if context.pending_input is not None
                        else None
                    ),
                },
                "updated_at": utc_now(),
            },
            deep=True,
        )
        await run_store.save(updated)

    async def _finalize_run(
        self,
        context: RuntimeContext,
        run_store: RunStore,
        event_store: EventStore,
    ) -> None:
        await self._save_snapshot(context, run_store)
        terminal_events = {
            ExecutionState.COMPLETED: EventType.RUN_COMPLETED,
            ExecutionState.FAILED: EventType.RUN_FAILED,
            ExecutionState.CANCELLED: EventType.RUN_CANCELLED,
            ExecutionState.LIMIT_REACHED: EventType.RUN_LIMIT_REACHED,
            ExecutionState.WAITING: EventType.RUN_WAITING,
        }
        try:
            event_type = terminal_events[context.state_machine.state]
        except KeyError as exc:
            raise InvalidStateTransitionError(
                f"cannot finalize state {context.state_machine.state.value}"
            ) from exc
        await event_store.emit(
            context.run_id,
            event_type,
            {
                "steps": context.step_count,
                "tool_calls": context.tool_call_count,
                "output": context.output,
                "error": context.error,
                "usage": context.usage.model_dump(mode="json"),
                "pending_input": (
                    context.pending_input.model_dump(mode="json")
                    if context.pending_input is not None
                    else None
                ),
            },
        )

    @staticmethod
    async def _apply_supervision_decision(
        context: RuntimeContext,
        decision: SupervisionDecision,
        event_store: EventStore,
        *,
        append_message: bool = True,
    ) -> None:
        await event_store.emit(
            context.run_id,
            EventType.SUPERVISOR_INTERVENED,
            decision.model_dump(mode="json"),
        )
        if append_message and decision.message:
            context.messages.append(Message.system(decision.message))
        if decision.action is SupervisionAction.STOP:
            if decision.terminal_status is None:
                raise InvalidStateTransitionError("stop decision has no terminal status")
            context.error = decision.reason
            context.state_machine.transition_to(ExecutionState(decision.terminal_status))

    @staticmethod
    def _build_result(context: RuntimeContext) -> AgentResult:
        status_map = {
            ExecutionState.COMPLETED: AgentResultStatus.COMPLETED,
            ExecutionState.FAILED: AgentResultStatus.FAILED,
            ExecutionState.CANCELLED: AgentResultStatus.CANCELLED,
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
            metadata={
                "run_id": str(context.run_id),
                "steps": context.step_count,
                "tool_calls": context.tool_call_count,
                "provider": context.provider_name,
                "pending_input": (
                    context.pending_input.model_dump(mode="json")
                    if context.pending_input is not None
                    else None
                ),
            },
        )
