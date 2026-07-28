"""Default bounded model -> tool -> model orchestration strategy."""

import logging
from time import monotonic

from base_agent.models import (
    EventType,
    Message,
    ModelRequest,
    ModelResponse,
    PendingInput,
    ToolCall,
    ToolDefinition,
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

logger = logging.getLogger(__name__)
_SUSPENDED_ACTION_BATCH_KEY = "model_tool_suspended_action_batch"

ToolObservation = tuple[ToolCall, ToolResult]


class ModelToolStrategy:
    """The reference ReAct-style loop used by AgentRuntime by default."""

    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        if await self._resume_action_batch(context, services):
            return
        context.step_count += 1
        await self._before_model_request(context, services)
        definitions = self._tool_definitions(context, services)
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
        model_started_at = monotonic()
        logger.info(
            "model request started",
            extra={
                "event": "model.request.started",
                "run_id": str(context.run_id),
                "step": context.step_count,
                "provider": services.provider.name,
                "model": context.profile.model,
                "message_count": len(request.messages),
                "tool_definition_count": len(request.tools),
            },
        )

        try:
            response = await services.provider.complete(request)
        except Exception as exc:
            logger.exception(
                "model request failed",
                extra={
                    "event": "model.request.failed",
                    "run_id": str(context.run_id),
                    "step": context.step_count,
                    "provider": services.provider.name,
                    "model": context.profile.model,
                    "duration_ms": round((monotonic() - model_started_at) * 1000, 3),
                    "error_type": type(exc).__name__,
                },
            )
            await self._fail_execution(
                context,
                services,
                f"model provider '{services.provider.name}' failed: {exc}",
            )
            return
        logger.info(
            "model request completed",
            extra={
                "event": "model.request.completed",
                "run_id": str(context.run_id),
                "step": context.step_count,
                "provider": services.provider.name,
                "model": context.profile.model,
                "duration_ms": round((monotonic() - model_started_at) * 1000, 3),
                "tool_call_count": len(response.tool_calls),
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.total_tokens,
            },
        )

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
            await self._on_iteration_completed(
                context,
                services,
                response,
                had_actions=False,
            )
            await self._complete_response(
                context,
                services,
                response.content or "",
            )
            return

        calls = self._select_tool_calls(context, response)
        await self._on_action_batch_selected(context, services, calls)
        await self._execute_tool_batch(
            context,
            services,
            calls,
            response=response,
        )

    async def _execute_tool_batch(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        calls: tuple[ToolCall, ...],
        *,
        response: ModelResponse,
        observations: list[ToolObservation] | None = None,
    ) -> None:
        executor = services.tool_executor or ToolExecutor(
            services.tool_registry or ToolRegistry()
        )
        collected = observations or []
        for index, call in enumerate(calls):
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
            tool_started_at = monotonic()
            logger.info(
                "tool execution started",
                extra={
                    "event": "tool.execution.started",
                    "run_id": str(context.run_id),
                    "step": context.step_count,
                    "tool_name": call.name,
                    "tool_call_id": call.id,
                },
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
            logger.info(
                "tool execution finished",
                extra={
                    "event": "tool.execution.finished",
                    "run_id": str(context.run_id),
                    "step": context.step_count,
                    "tool_name": call.name,
                    "tool_call_id": call.id,
                    "status": result.status.value,
                    "error_code": result.error_code,
                    "duration_ms": round((monotonic() - tool_started_at) * 1000, 3),
                },
            )
            if result.status is ToolResultStatus.WAITING:
                context.supervision_data[_SUSPENDED_ACTION_BATCH_KEY] = {
                    "waiting_call": call.model_dump(mode="json"),
                    "remaining_calls": [
                        item.model_dump(mode="json") for item in calls[index + 1 :]
                    ],
                    "observations": [
                        {
                            "call": observed_call.model_dump(mode="json"),
                            "result": observed_result.model_dump(mode="json"),
                        }
                        for observed_call, observed_result in collected
                    ],
                    "response": response.model_dump(mode="json"),
                }
                wait_data = result.data if isinstance(result.data, dict) else {}
                prompt = wait_data.get("prompt")
                if not isinstance(prompt, str) or not prompt.strip():
                    logger.error(
                        "tool returned invalid input request",
                        extra={
                            "event": "tool.waiting.invalid",
                            "run_id": str(context.run_id),
                            "step": context.step_count,
                            "tool_name": call.name,
                            "tool_call_id": call.id,
                        },
                    )
                    await self._fail_execution(
                        context,
                        services,
                        f"tool '{call.name}' returned an invalid input request",
                    )
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
                await self._wait_for_input(context, services)
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
            collected.append((call, result))
            decision = await services.supervisor.after_tool(context, call, result)
            if decision.action is not SupervisionAction.CONTINUE:
                await apply_supervision_decision(context, decision, services.event_store)
                await save_context_snapshot(context, services.run_store)
                return
            if await self._cancel_if_requested(context, services):
                return
        await self._on_observation_batch_recorded(
            context,
            services,
            tuple(collected),
        )
        await self._on_iteration_completed(
            context,
            services,
            response,
            had_actions=True,
        )
        await save_context_snapshot(context, services.run_store)

    async def _resume_action_batch(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> bool:
        raw = context.supervision_data.pop(_SUSPENDED_ACTION_BATCH_KEY, None)
        if raw is None:
            return False
        try:
            if not isinstance(raw, dict):
                raise TypeError("suspended action batch must be an object")
            waiting_call = ToolCall.model_validate(raw["waiting_call"])
            remaining = tuple(
                ToolCall.model_validate(item) for item in raw["remaining_calls"]
            )
            response = ModelResponse.model_validate(raw["response"])
            observations = [
                (
                    ToolCall.model_validate(item["call"]),
                    ToolResult.model_validate(item["result"]),
                )
                for item in raw["observations"]
            ]
            waiting_result = self._find_tool_result(context, waiting_call.id)
        except (KeyError, TypeError, ValueError) as exc:
            await self._fail_execution(
                context,
                services,
                f"invalid suspended action batch: {exc}",
            )
            return True

        observations.append((waiting_call, waiting_result))
        if remaining:
            await self._execute_tool_batch(
                context,
                services,
                remaining,
                response=response,
                observations=observations,
            )
            return True
        await self._on_observation_batch_recorded(
            context,
            services,
            tuple(observations),
        )
        await self._on_iteration_completed(
            context,
            services,
            response,
            had_actions=True,
        )
        await save_context_snapshot(context, services.run_store)
        return True

    @staticmethod
    def _has_suspended_action_batch(context: RuntimeContext) -> bool:
        return _SUSPENDED_ACTION_BATCH_KEY in context.supervision_data

    @staticmethod
    def _find_tool_result(context: RuntimeContext, tool_call_id: str) -> ToolResult:
        for message in reversed(context.messages):
            if message.tool_call_id == tool_call_id and message.content is not None:
                return ToolResult.model_validate_json(message.content)
        raise ValueError(f"missing resumed ToolResult for call '{tool_call_id}'")

    def _select_tool_calls(
        self,
        context: RuntimeContext,
        response: ModelResponse,
    ) -> tuple[ToolCall, ...]:
        del context
        return response.tool_calls

    async def _before_model_request(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        del context, services

    async def _on_action_batch_selected(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        calls: tuple[ToolCall, ...],
    ) -> None:
        del context, services, calls

    async def _on_observation_batch_recorded(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        observations: tuple[ToolObservation, ...],
    ) -> None:
        del context, services, observations

    async def _on_iteration_completed(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        response: ModelResponse,
        *,
        had_actions: bool,
    ) -> None:
        del context, services, response, had_actions

    def _tool_definitions(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> tuple[ToolDefinition, ...]:
        return (
            services.tool_registry.definitions(context.enabled_tool_names)
            if services.tool_registry is not None
            else ()
        )

    async def _complete_response(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        content: str,
    ) -> None:
        del services
        context.output = content
        context.state_machine.transition_to(ExecutionState.COMPLETED)

    async def _fail_execution(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        error: str,
    ) -> None:
        del services
        context.error = error
        if context.state is ExecutionState.RUNNING:
            context.state_machine.transition_to(ExecutionState.FAILED)

    async def _wait_for_input(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        del context, services

    @staticmethod
    async def _cancel_if_requested(
        context: RuntimeContext, services: RuntimeServices
    ) -> bool:
        if not await services.run_store.is_cancel_requested(context.run_id):
            return False
        context.error = "run cancellation requested"
        context.state_machine.transition_to(ExecutionState.CANCELLED)
        return True
