"""Planner/ReAct-style execution with immutable completed-step history."""

from __future__ import annotations

import json

from base_agent.models import (
    EventType,
    ExecutionPlan,
    Message,
    ModelResponse,
    PlanStatus,
    PlanStep,
    StepStatus,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from base_agent.orchestration.model_tool import ModelToolStrategy
from base_agent.orchestration.planning import update_execution_plan
from base_agent.orchestration.protocol import RuntimeServices
from base_agent.orchestration.react import ReActResult
from base_agent.runtime.context import RuntimeContext
from base_agent.runtime.persistence import save_context_snapshot
from base_agent.runtime.state_machine import ExecutionState

_STATE_KEY = "built_in_planning"
_PHASE_PLANNING = "planning"
_PHASE_EXECUTING = "executing"
_PHASE_UPDATING = "updating"
_PHASE_SUMMARIZING = "summarizing"
_SUPPORTED_EXECUTORS = {None, "model", "react"}
_MAX_RESULT_CHARACTERS = 12_000


class PlanningStrategy(ModelToolStrategy):
    """Execute a Step, replan pending work, and preserve settled Step history."""

    def __init__(
        self,
        *,
        summarize: bool = True,
        replan_after_step: bool = True,
        max_react_iterations_per_step: int = 20,
    ) -> None:
        if max_react_iterations_per_step < 1:
            raise ValueError("max_react_iterations_per_step must be at least 1")
        self.summarize = summarize
        self.replan_after_step = replan_after_step
        self.max_react_iterations_per_step = max_react_iterations_per_step

    async def settle(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        """Align an active Plan when the Runtime stops outside strategy code."""

        await self._settle_terminal_plan(context, services)

    async def advance(self, context: RuntimeContext, services: RuntimeServices) -> None:
        state = context.supervision_data.setdefault(
            _STATE_KEY,
            {
                "phase": (
                    _PHASE_PLANNING
                    if context.plan is None and context.planning_requested
                    else _PHASE_EXECUTING
                )
            },
        )

        if context.plan is None:
            if not context.planning_requested:
                raise ValueError("PlanningStrategy requires an ExecutionPlan")
            if state.get("phase") != _PHASE_PLANNING:
                await self._fail_execution(
                    context,
                    services,
                    "planning state is inconsistent with the missing plan",
                )
                return
            if not state.get("prompt_added"):
                context.messages.append(
                    Message.user(self._planning_prompt(context, services))
                )
                state["prompt_added"] = True
                await save_context_snapshot(context, services.run_store)
            await super().advance(context, services)
            await self._settle_terminal_plan(context, services)
            return

        plan = self._require_plan(context)
        self._validate_executors(plan)
        if state.get("phase") == _PHASE_SUMMARIZING:
            await super().advance(context, services)
            await self._settle_terminal_plan(context, services)
            return
        if state.get("phase") == _PHASE_UPDATING:
            await super().advance(context, services)
            await self._settle_terminal_plan(context, services)
            return

        waiting = self._step_with_status(plan, StepStatus.WAITING)
        if waiting is not None:
            plan = plan.resume_step(waiting.id)
            await update_execution_plan(
                context,
                services,
                plan,
                emit_plan_updated=False,
            )

        running = self._step_with_status(plan, StepStatus.RUNNING)
        if running is None:
            ready = plan.ready_steps()
            if not ready:
                await self._handle_no_ready_step(context, services, plan)
                return
            running = ready[0]
            plan = plan.start_step(running.id)
            await update_execution_plan(
                context,
                services,
                plan,
                emit_plan_updated=False,
            )
            prompt = (
                self._react_step_prompt(context, plan, running)
                if running.executor == "react"
                else self._step_prompt(context, plan, running)
            )
            context.messages.append(Message.user(prompt))
            await save_context_snapshot(context, services.run_store)

        if running.executor == "react":
            raw_react = state.get("react")
            react = raw_react if isinstance(raw_react, dict) else {}
            if (
                int(react.get("iteration", 0))
                >= self.max_react_iterations_per_step
                and not self._has_suspended_action_batch(context)
            ):
                await self._fail_execution(
                    context,
                    services,
                    (
                        "ReAct iteration limit reached for "
                        f"step '{running.id}' ({self.max_react_iterations_per_step})"
                    ),
                )
                return
        await super().advance(context, services)
        await self._settle_terminal_plan(context, services)

    def _tool_definitions(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> tuple[ToolDefinition, ...]:
        state = context.supervision_data.get(_STATE_KEY, {})
        if state.get("phase") in {
            _PHASE_PLANNING,
            _PHASE_UPDATING,
            _PHASE_SUMMARIZING,
        }:
            return ()
        return super()._tool_definitions(context, services)

    async def _complete_response(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        content: str,
    ) -> None:
        state = context.supervision_data.setdefault(
            _STATE_KEY,
            {"phase": _PHASE_EXECUTING},
        )
        if state.get("phase") == _PHASE_PLANNING:
            try:
                plan = self._parse_generated_plan(content, services.provider.name)
                self._validate_executors(plan)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                await self._fail_execution(
                    context,
                    services,
                    f"model generated an invalid execution plan: {exc}",
                )
                return
            await update_execution_plan(context, services, plan)
            state["phase"] = _PHASE_EXECUTING
            return
        if state.get("phase") == _PHASE_UPDATING:
            plan = self._require_plan(context)
            try:
                future_steps = self._parse_replanned_steps(content)
                changed = not self._future_steps_unchanged(plan, future_steps)
                updated = (
                    self._merge_replanned_steps(plan, future_steps)
                    if changed
                    else plan
                )
                self._validate_executors(updated)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                await self._fail_execution(
                    context,
                    services,
                    f"model generated an invalid updated execution plan: {exc}",
                )
                return
            await services.event_store.emit(
                context.run_id,
                EventType.PLAN_REVIEWED,
                {
                    "plan_id": plan.id,
                    "revision": plan.revision,
                    "changed": changed,
                    "completed_step_id": state.get("completed_step_id"),
                    "proposed_steps": [
                        step.model_dump(mode="json") for step in future_steps
                    ],
                },
            )
            if changed:
                await update_execution_plan(context, services, updated)
            state["phase"] = _PHASE_EXECUTING
            state.pop("completed_step_id", None)
            if updated.status is PlanStatus.COMPLETED:
                await self._finish_plan(context, services, updated)
            return
        if state.get("phase") == _PHASE_SUMMARIZING:
            context.output = content
            context.state_machine.transition_to(ExecutionState.COMPLETED)
            return

        plan = self._require_plan(context)
        running = self._step_with_status(plan, StepStatus.RUNNING)
        if running is None:
            await self._fail_execution(
                context,
                services,
                "planning strategy received a step result without an active step",
            )
            return
        if running.executor == "react":
            try:
                react_result = self._parse_react_result(content)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                await self._fail_execution(
                    context,
                    services,
                    f"model generated an invalid ReAct step result: {exc}",
                )
                return
            react_state = state.get("react")
            iterations = (
                react_state.get("iteration", 0)
                if isinstance(react_state, dict)
                else 0
            )
            completed = plan.complete_step(
                running.id,
                react_result.result,
                success=react_result.success,
                attachments=react_result.attachments,
                metadata={"react_iterations": iterations},
            )
            state.pop("react", None)
        else:
            completed = plan.complete_step(running.id, content)
        await update_execution_plan(
            context,
            services,
            completed,
            emit_plan_updated=False,
        )
        if self.replan_after_step:
            state["phase"] = _PHASE_UPDATING
            state["completed_step_id"] = running.id
            context.messages.append(
                Message.user(self._replanning_prompt(context, services, completed, running.id))
            )
            await save_context_snapshot(context, services.run_store)
            return
        if completed.status is not PlanStatus.COMPLETED:
            return
        await self._finish_plan(context, services, completed)

    async def _fail_execution(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        error: str,
    ) -> None:
        plan = context.plan
        if plan is not None and plan.status not in {
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
        }:
            await update_execution_plan(
                context,
                services,
                plan.fail(error),
                emit_plan_updated=False,
            )
        await super()._fail_execution(context, services, error)

    async def _wait_for_input(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        plan = self._require_plan(context)
        running = self._step_with_status(plan, StepStatus.RUNNING)
        if running is None:
            await self._fail_execution(
                context,
                services,
                "planning strategy cannot suspend without an active step",
            )
            return
        await update_execution_plan(
            context,
            services,
            plan.wait_step(running.id),
            emit_plan_updated=False,
        )

    async def _settle_terminal_plan(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        plan = context.plan
        if plan is None or plan.status in {
            PlanStatus.COMPLETED,
            PlanStatus.FAILED,
            PlanStatus.CANCELLED,
        }:
            return
        if context.state is ExecutionState.CANCELLED:
            await update_execution_plan(
                context,
                services,
                plan.cancel(context.error or "run cancelled"),
                emit_plan_updated=False,
            )
        elif context.state in {ExecutionState.FAILED, ExecutionState.LIMIT_REACHED}:
            await update_execution_plan(
                context,
                services,
                plan.fail(context.error or f"run ended with {context.state.value}"),
                emit_plan_updated=False,
            )

    async def _handle_no_ready_step(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        plan: ExecutionPlan,
    ) -> None:
        if plan.status is PlanStatus.COMPLETED:
            await self._finish_plan(context, services, plan)
            return
        await self._fail_execution(
            context,
            services,
            "execution plan has no ready or active step",
        )

    async def _finish_plan(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        plan: ExecutionPlan,
    ) -> None:
        if not self.summarize:
            context.output = self._deterministic_summary(plan)
            context.state_machine.transition_to(ExecutionState.COMPLETED)
            return
        context.supervision_data[_STATE_KEY] = {"phase": _PHASE_SUMMARIZING}
        context.messages.append(Message.user(self._summary_prompt(context, plan)))
        await save_context_snapshot(context, services.run_store)

    async def _before_model_request(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> None:
        step = self._active_react_step(context)
        if step is None:
            return
        state = context.supervision_data[_STATE_KEY]
        raw_react = state.get("react")
        react = raw_react if isinstance(raw_react, dict) else {}
        if react.get("step_id") != step.id:
            react = {"step_id": step.id, "iteration": 0}
            state["react"] = react
        react["iteration"] = int(react.get("iteration", 0)) + 1
        await services.event_store.emit(
            context.run_id,
            EventType.REACT_ITERATION_STARTED,
            self._react_event_data(context, step, react),
        )

    async def _on_action_batch_selected(
        self,
        context: RuntimeContext,
        services: RuntimeServices,
        calls: tuple[ToolCall, ...],
    ) -> None:
        step = self._active_react_step(context)
        if step is None:
            return
        data = self._react_event_data(
            context,
            step,
            context.supervision_data[_STATE_KEY].get("react", {}),
        )
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
        step = self._active_react_step(context)
        if step is None:
            return
        data = self._react_event_data(
            context,
            step,
            context.supervision_data[_STATE_KEY].get("react", {}),
        )
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
        step = self._active_react_step(context)
        if step is None:
            return
        data = self._react_event_data(
            context,
            step,
            context.supervision_data[_STATE_KEY].get("react", {}),
        )
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

    @staticmethod
    def _require_plan(context: RuntimeContext) -> ExecutionPlan:
        if context.plan is None:
            raise ValueError("PlanningStrategy requires an ExecutionPlan")
        return context.plan

    @staticmethod
    def _validate_executors(plan: ExecutionPlan) -> None:
        unsupported = {
            step.executor
            for step in plan.steps
            if step.executor not in _SUPPORTED_EXECUTORS
        }
        if unsupported:
            names = ", ".join(sorted(str(item) for item in unsupported))
            raise ValueError(
                f"unsupported plan step executors: {names}; "
                "built-in PlanningStrategy supports 'model' or 'react'"
            )

    @staticmethod
    def _parse_generated_plan(content: str, provider_name: str) -> ExecutionPlan:
        payload = PlanningStrategy._parse_json_object(content)
        if not isinstance(payload, dict):
            raise TypeError("plan response must be a JSON object")
        plan = ExecutionPlan.model_validate(payload)
        if plan.status is not PlanStatus.CREATED:
            raise ValueError("generated plan must start in created status")
        if any(step.status is not StepStatus.PENDING for step in plan.steps):
            raise ValueError("generated plan steps must start in pending status")
        return plan.model_copy(
            update={
                "metadata": {
                    **plan.metadata,
                    "generated": True,
                    "provider": provider_name,
                }
            }
        )

    @staticmethod
    def _parse_replanned_steps(content: str) -> tuple[PlanStep, ...]:
        payload = PlanningStrategy._parse_json_object(content)
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raise TypeError("updated plan response must contain a JSON steps array")
        steps = tuple(PlanStep.model_validate(step) for step in raw_steps)
        if any(step.status is not StepStatus.PENDING for step in steps):
            raise ValueError("updated plan steps must start in pending status")
        return steps

    @staticmethod
    def _parse_react_result(content: str) -> ReActResult:
        return ReActResult.model_validate(
            PlanningStrategy._parse_json_object(content)
        )

    @staticmethod
    def _parse_json_object(content: str) -> dict[str, object]:
        candidate = content.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if len(lines) >= 3 and lines[-1].strip() == "```":
                candidate = "\n".join(lines[1:-1])
                if candidate.lstrip().startswith("json"):
                    candidate = candidate.lstrip()[4:].lstrip()
        payload = json.loads(candidate)
        if not isinstance(payload, dict):
            raise TypeError("model response must be a JSON object")
        return payload

    @staticmethod
    def _merge_replanned_steps(
        plan: ExecutionPlan,
        future_steps: tuple[PlanStep, ...],
    ) -> ExecutionPlan:
        """Keep every settled Step byte-for-byte and replace only pending work."""

        historical_statuses = {
            StepStatus.COMPLETED,
            StepStatus.FAILED,
            StepStatus.CANCELLED,
            StepStatus.SKIPPED,
        }
        history = tuple(
            step for step in plan.steps if step.status in historical_statuses
        )
        if not any(step.status is StepStatus.COMPLETED for step in history):
            raise ValueError("updated plan requires at least one completed historical step")
        status = PlanStatus.RUNNING if future_steps else PlanStatus.COMPLETED
        payload = plan.model_dump(mode="python")
        payload.update(
            {
                "steps": history + future_steps,
                "status": status,
                "revision": plan.revision + 1,
                "metadata": {
                    **plan.metadata,
                    "replan_count": int(plan.metadata.get("replan_count", 0)) + 1,
                },
            }
        )
        updated = ExecutionPlan.model_validate(payload)
        for old_step, retained_step in zip(history, updated.steps, strict=False):
            if old_step != retained_step:
                raise ValueError("updated plan changed settled step history")
        return updated

    @staticmethod
    def _future_steps_unchanged(
        plan: ExecutionPlan,
        future_steps: tuple[PlanStep, ...],
    ) -> bool:
        current_future = tuple(
            step for step in plan.steps if step.status is StepStatus.PENDING
        )
        return current_future == future_steps

    @staticmethod
    def _step_with_status(
        plan: ExecutionPlan,
        status: StepStatus,
    ) -> PlanStep | None:
        return next((step for step in plan.steps if step.status is status), None)

    @classmethod
    def _active_react_step(cls, context: RuntimeContext) -> PlanStep | None:
        state = context.supervision_data.get(_STATE_KEY, {})
        if state.get("phase") != _PHASE_EXECUTING or context.plan is None:
            return None
        running = cls._step_with_status(context.plan, StepStatus.RUNNING)
        if running is None or running.executor != "react":
            return None
        return running

    @staticmethod
    def _react_event_data(
        context: RuntimeContext,
        step: PlanStep,
        react_state: object,
    ) -> dict[str, object]:
        state = react_state if isinstance(react_state, dict) else {}
        return {
            "plan_id": context.plan.id if context.plan is not None else None,
            "step_id": step.id,
            "iteration": state.get("iteration", 0),
        }

    @staticmethod
    def _step_prompt(
        context: RuntimeContext,
        plan: ExecutionPlan,
        step: PlanStep,
    ) -> str:
        dependencies = [
            {
                "id": item.id,
                "description": item.description,
                "result": item.result,
            }
            for item in plan.steps
            if item.id in step.dependencies
        ]
        return "\n".join(
            [
                "Execute exactly one step from the active plan.",
                f"Original task: {context.input_text}",
                f"Plan: {plan.title}",
                f"Current step [{step.id}]: {step.description}",
                (
                    "Completed dependency results: "
                    + json.dumps(dependencies, ensure_ascii=False, default=str)
                    if dependencies
                    else "Completed dependency results: none"
                ),
                (
                    "Use the available tools when needed. Finish this step only, "
                    "then return its concrete result."
                ),
            ]
        )

    @staticmethod
    def _react_step_prompt(
        context: RuntimeContext,
        plan: ExecutionPlan,
        step: PlanStep,
    ) -> str:
        dependencies = [
            {
                "id": item.id,
                "description": item.description,
                "result": item.result,
            }
            for item in plan.steps
            if item.id in step.dependencies
        ]
        return "\n".join(
            [
                "Execute one plan step with a ReAct loop.",
                f"Original task: {context.input_text}",
                f"Plan: {plan.title}",
                f"Current step [{step.id}]: {step.description}",
                (
                    "Completed dependency results: "
                    + json.dumps(dependencies, ensure_ascii=False, default=str)
                    if dependencies
                    else "Completed dependency results: none"
                ),
                (
                    "Analyze the current state, select the tool actions needed, observe "
                    "their results, and iterate until this step is complete. Independent "
                    "tool calls may be selected in one action batch; dependent calls must "
                    "wait for a later iteration."
                ),
                (
                    "Do not reveal private chain-of-thought. Tool calls communicate "
                    "actions. When complete, return JSON only as "
                    '{"success":true,"result":"concrete step result",'
                    '"attachments":[]}.'
                ),
            ]
        )

    @staticmethod
    def _planning_prompt(
        context: RuntimeContext,
        services: RuntimeServices,
    ) -> str:
        tools = (
            services.tool_registry.definitions(context.enabled_tool_names)
            if services.tool_registry is not None
            else ()
        )
        capabilities = [
            {"name": tool.name, "description": tool.description} for tool in tools
        ]
        return "\n".join(
            [
                "Create a concise execution plan for the original user task.",
                f"Original task: {context.input_text}",
                (
                    "Available tools: "
                    + json.dumps(capabilities, ensure_ascii=False)
                ),
                (
                    "Return JSON only with this shape: "
                    '{"id":"plan-id","title":"title","steps":['
                    '{"id":"step-id","description":"atomic action",'
                    '"executor":"react","dependencies":[]}]}'
                ),
                (
                    "Use stable alphanumeric step ids. Steps must be atomic, executable "
                    "through the available tools, dependency-ordered, and free of cycles. "
                    "Use executor 'react' for steps that may need tools or iterative "
                    "observation, and 'model' for direct tool-free generation. Use one "
                    "step when decomposition is unnecessary."
                ),
            ]
        )

    @staticmethod
    def _replanning_prompt(
        context: RuntimeContext,
        services: RuntimeServices,
        plan: ExecutionPlan,
        completed_step_id: str,
    ) -> str:
        completed_step = next(
            step for step in plan.steps if step.id == completed_step_id
        )
        tools = (
            services.tool_registry.definitions(context.enabled_tool_names)
            if services.tool_registry is not None
            else ()
        )
        capabilities = [
            {"name": tool.name, "description": tool.description} for tool in tools
        ]
        return "\n".join(
            [
                "Update the execution plan after one completed step.",
                f"Original task: {context.input_text}",
                f"Current plan: {plan.model_dump_json()}",
                f"Just completed step: {completed_step.model_dump_json()}",
                "Available tools: " + json.dumps(capabilities, ensure_ascii=False),
                (
                    "Return JSON only as {\"steps\":[...]}. The steps array represents "
                    "all remaining work and replaces every not-yet-executed step."
                ),
                (
                    "Do not include completed, failed, cancelled, or skipped steps; the "
                    "runtime preserves that immutable history. Return an empty steps "
                    "array when the original task is complete."
                ),
                (
                    "Each new step must use pending status, have a unique stable id, and "
                    "may depend on preserved historical step ids. Dependencies must be "
                    "valid and acyclic. Use executor 'react' for steps that may need "
                    "tools or iterative observation, and 'model' for direct tool-free "
                    "generation."
                ),
            ]
        )

    @classmethod
    def _summary_prompt(
        cls,
        context: RuntimeContext,
        plan: ExecutionPlan,
    ) -> str:
        results = cls._bounded_results(plan)
        return "\n".join(
            [
                "The execution plan is complete. Deliver the final result to the user.",
                f"Original task: {context.input_text}",
                f"Plan: {plan.title}",
                f"Step results:\n{results}",
                (
                    "Synthesize the completed work into one clear final answer. "
                    "Do not return a todo list or another plan."
                ),
            ]
        )

    @classmethod
    def _deterministic_summary(cls, plan: ExecutionPlan) -> str:
        return f"{plan.title}\n\n{cls._bounded_results(plan)}"

    @staticmethod
    def _bounded_results(plan: ExecutionPlan) -> str:
        rendered: list[str] = []
        remaining = _MAX_RESULT_CHARACTERS
        for step in plan.steps:
            value = str(step.result) if step.result is not None else ""
            line = f"- [{step.id}] {step.description}: {value}"
            if len(line) > remaining:
                line = line[: max(remaining - 1, 0)] + "…"
            rendered.append(line)
            remaining -= len(line)
            if remaining <= 0:
                break
        return "\n".join(rendered)
