"""ReAct behavior layered on the shared Model/Tool execution loop."""

import json

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models import EventType, Message, ModelResponse, ToolCall, ToolResult
from base_agent.orchestration.model_tool import ModelToolStrategy
from base_agent.orchestration.protocol import RuntimeServices
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.persistence import save_context_snapshot
from base_agent.runtime.state_machine import ExecutionState

_STATE_KEY = "react_strategy"


class ReActResult(BaseModel):
    """Structured terminal result of one ReAct task or Plan Step."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    result: str
    attachments: tuple[str, ...] = Field(default_factory=tuple)


class ReActStrategy(ModelToolStrategy):
    """Run an entire Agent task as observable ReAct iterations."""

    def __init__(self, *, max_iterations: int = 20) -> None:
        if max_iterations < 1:
            raise ValueError("max_iterations must be at least 1")
        self.max_iterations = max_iterations

    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        state = context.supervision_data.setdefault(
            _STATE_KEY,
            {"iteration": 0, "prompt_added": False},
        )
        if not state.get("prompt_added"):
            first = context.messages[0]
            if first.content is None:
                raise ValueError("ReActStrategy requires a textual System Prompt")
            context.messages[0] = Message.system(
                first.content + "\n\n" + self._instructions()
            )
            state["prompt_added"] = True
            await save_context_snapshot(context, services.run_store)
        if (
            int(state.get("iteration", 0)) >= self.max_iterations
            and not self._has_suspended_action_batch(context)
        ):
            await self._fail_execution(
                context,
                services,
                f"ReAct iteration limit reached ({self.max_iterations})",
            )
            return
        await super().advance(context, services)

    async def _before_model_request(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        state = context.supervision_data[_STATE_KEY]
        state["iteration"] = int(state.get("iteration", 0)) + 1
        await services.event_store.emit(
            context.run_id,
            EventType.REACT_ITERATION_STARTED,
            self._event_data(state),
        )

    async def _on_action_batch_selected(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        calls: tuple[ToolCall, ...],
    ) -> None:
        data = self._event_data(context.supervision_data[_STATE_KEY])
        data["actions"] = [
            {"call_id": call.id, "tool_name": call.name} for call in calls
        ]
        await services.event_store.emit(
            context.run_id,
            EventType.REACT_ACTION_BATCH_SELECTED,
            data,
        )

    async def _on_observation_batch_recorded(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        observations: tuple[tuple[ToolCall, ToolResult], ...],
    ) -> None:
        data = self._event_data(context.supervision_data[_STATE_KEY])
        data["observations"] = [
            {
                "call_id": call.id,
                "tool_name": call.name,
                "status": result.status.value,
                "error_code": result.error_code,
            }
            for call, result in observations
        ]
        await services.event_store.emit(
            context.run_id,
            EventType.REACT_OBSERVATION_BATCH_RECORDED,
            data,
        )

    async def _on_iteration_completed(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        response: ModelResponse,
        *,
        had_actions: bool,
    ) -> None:
        data = self._event_data(context.supervision_data[_STATE_KEY])
        data.update(
            {
                "had_actions": had_actions,
                "finish_reason": response.finish_reason,
            }
        )
        await services.event_store.emit(
            context.run_id,
            EventType.REACT_ITERATION_COMPLETED,
            data,
        )

    async def _complete_response(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        content: str,
    ) -> None:
        try:
            result = self._parse_result(content)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            await self._fail_execution(
                context,
                services,
                f"model generated an invalid ReAct result: {exc}",
            )
            return
        state = context.supervision_data[_STATE_KEY]
        state["result"] = result.model_dump(mode="json")
        context.output = result.result
        context.state_machine.transition_to(ExecutionState.COMPLETED)

    @staticmethod
    def _parse_result(content: str) -> ReActResult:
        candidate = content.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                candidate = "\n".join(lines[1:-1])
                if candidate.lstrip().startswith("json"):
                    candidate = candidate.lstrip()[4:].lstrip()
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise TypeError("ReAct response must be a JSON object")
        return ReActResult.model_validate(payload)

    @staticmethod
    def _event_data(state: dict[str, object]) -> dict[str, object]:
        return {"iteration": state.get("iteration", 0)}

    @staticmethod
    def _instructions() -> str:
        return "\n".join(
            [
                "Use an iterative ReAct process to complete the task.",
                (
                    "Analyze the current state, choose the necessary tool actions, "
                    "observe their results, and repeat until the task is complete."
                ),
                (
                    "Independent tool calls may be emitted in one action batch. Calls "
                    "that depend on an earlier result must wait for a later iteration."
                ),
                (
                    "Do not reveal private chain-of-thought. Tool calls express actions. "
                    "When complete, return JSON only as "
                    '{"success":true,"result":"final result","attachments":[]}.'
                ),
            ]
        )
