"""Default bounded model -> tool -> model orchestration strategy."""

from base_agent.models import (
    EventType,
    Message,
    ModelRequest,
    PendingInput,
    ToolResult,
    ToolResultStatus,
)
from base_agent.orchestration.protocol import RuntimeServices
from base_agent.orchestration.supervision import apply_supervision_decision
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.persistence import save_context_snapshot
from base_agent.runtime.state_machine import ExecutionState
from base_agent.supervision import SupervisionAction
from base_agent.tools import ToolContext, ToolExecutor, ToolRegistry


class ModelToolStrategy:
    """The reference ReAct-style loop used by AgentRuntime by default."""

    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        context.step_count += 1
        definitions = (
            services.tool_registry.definitions(context.enabled_tool_names)
            if services.tool_registry is not None
            else ()
        )
        request = ModelRequest(
            messages=tuple(context.messages),
            tools=definitions,
            model=context.profile.model,
            attachments=context.attachments,
            memories=context.memories,
        )
        await services.event_store.emit(
            context.run_id,
            EventType.MODEL_REQUESTED,
            {
                "step": context.step_count,
                "request": {
                    **request.model_dump(mode="json", exclude={"memories"}),
                    "memories": [
                        {"id": str(match.record.id), "score": match.score}
                        for match in request.memories
                    ],
                },
            },
        )

        try:
            response = await services.provider.complete(request)
        except Exception as exc:
            context.error = f"model provider '{services.provider.name}' failed: {exc}"
            context.state_machine.transition_to(ExecutionState.FAILED)
            return

        await services.event_store.emit(
            context.run_id,
            EventType.MODEL_RESPONDED,
            {"step": context.step_count, "response": response.model_dump(mode="json")},
        )
        context.responses.append(response)
        context.usage = context.usage + response.usage
        context.messages.append(response.to_assistant_message())
        await save_context_snapshot(context, services.run_store)

        if await self._cancel_if_requested(context, services):
            return
        if not response.tool_calls:
            context.output = response.content
            context.state_machine.transition_to(ExecutionState.COMPLETED)
            return

        executor = services.tool_executor or ToolExecutor(
            services.tool_registry or ToolRegistry()
        )
        for call in response.tool_calls:
            if await self._cancel_if_requested(context, services):
                return
            call_data = {"step": context.step_count, "call": call.model_dump(mode="json")}
            await services.event_store.emit(
                context.run_id, EventType.TOOL_REQUESTED, call_data
            )
            decision = await services.supervisor.before_tool(context, call)
            if decision.action is not SupervisionAction.CONTINUE:
                await apply_supervision_decision(
                    context,
                    decision,
                    services.event_store,
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
                await services.event_store.emit(
                    context.run_id,
                    EventType.TOOL_FAILED,
                    {
                        "step": context.step_count,
                        "call_id": call.id,
                        "result": blocked.model_dump(mode="json"),
                    },
                )
                await save_context_snapshot(context, services.run_store)
                return

            context.tool_call_count += 1
            await save_context_snapshot(context, services.run_store)
            await services.event_store.emit(
                context.run_id, EventType.TOOL_STARTED, call_data
            )
            result = await executor.execute(
                call,
                granted_permissions=context.profile.permissions,
                allowed_tools=frozenset(context.enabled_tool_names),
                context=ToolContext(
                    run_id=context.run_id,
                    resources=services.resources,
                    artifacts=services.artifacts,
                    memories=services.memories,
                ),
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
                await services.event_store.emit(
                    context.run_id,
                    EventType.TOOL_WAITING,
                    {
                        "step": context.step_count,
                        "call_id": call.id,
                        "result": result.model_dump(mode="json"),
                    },
                )
                await save_context_snapshot(context, services.run_store)
                return

            result_event = (
                EventType.TOOL_COMPLETED
                if result.status is ToolResultStatus.SUCCESS
                else EventType.TOOL_FAILED
            )
            await services.event_store.emit(
                context.run_id,
                result_event,
                {
                    "step": context.step_count,
                    "call_id": call.id,
                    "result": result.model_dump(mode="json"),
                },
            )
            context.messages.append(Message.tool(result.model_dump_json(), tool_call_id=call.id))
            decision = await services.supervisor.after_tool(context, call, result)
            if decision.action is not SupervisionAction.CONTINUE:
                await apply_supervision_decision(context, decision, services.event_store)
                await save_context_snapshot(context, services.run_store)
                return
            if await self._cancel_if_requested(context, services):
                return

    @staticmethod
    async def _cancel_if_requested(
        context: RuntimeContext, services: RuntimeServices
    ) -> bool:
        if not await services.run_store.is_cancel_requested(context.run_id):
            return False
        context.error = "run cancellation requested"
        context.state_machine.transition_to(ExecutionState.CANCELLED)
        return True
