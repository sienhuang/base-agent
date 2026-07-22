"""Built-in stateless policies that keep all per-run data in RuntimeContext."""

import json

from base_agent.models import RunStatus, ToolCall, ToolResult, ToolResultStatus
from base_agent.profiles import AgentProfile
from base_agent.runtime.context import RuntimeContext
from base_agent.supervision.composite import CompositeSupervisor
from base_agent.supervision.decision import SupervisionDecision
from base_agent.supervision.protocol import BaseSupervisor


class ExecutionBudget(BaseSupervisor):
    name = "execution-budget"

    async def before_model(self, context: RuntimeContext) -> SupervisionDecision:
        if context.step_count >= context.profile.max_steps:
            return SupervisionDecision.stop(
                self.name,
                reason=f"maximum model steps reached ({context.profile.max_steps})",
                terminal_status=RunStatus.LIMIT_REACHED,
                metadata={"limit": context.profile.max_steps, "kind": "model_steps"},
            )
        return await super().before_model(context)

    async def before_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
    ) -> SupervisionDecision:
        if context.tool_call_count >= context.profile.max_tool_calls:
            return SupervisionDecision.stop(
                self.name,
                reason=f"maximum tool calls reached ({context.profile.max_tool_calls})",
                terminal_status=RunStatus.LIMIT_REACHED,
                metadata={"limit": context.profile.max_tool_calls, "kind": "tool_calls"},
            )
        return await super().before_tool(context, call)


class DuplicateToolCallDetector(BaseSupervisor):
    name = "duplicate-tool-call-detector"
    _state_key = "duplicate_tool_calls"

    async def before_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
    ) -> SupervisionDecision:
        fingerprint = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
        state = context.supervision_data.setdefault(self._state_key, {})
        previous = state.get("fingerprint")
        count = int(state.get("count", 0)) + 1 if previous == fingerprint else 1
        state.update({"fingerprint": fingerprint, "count": count})
        if count >= context.profile.duplicate_tool_call_threshold:
            return SupervisionDecision.redirect(
                self.name,
                reason=f"repeated identical tool call detected ({count} times)",
                message=(
                    "The same tool call has been repeated without enough progress. "
                    "Do not repeat it again; inspect prior observations and choose a new strategy."
                ),
                metadata={"count": count, "tool": call.name},
            )
        return await super().before_tool(context, call)


class NoProgressDetector(BaseSupervisor):
    name = "no-progress-detector"
    _state_key = "consecutive_tool_failures"

    async def after_tool(
        self,
        context: RuntimeContext,
        call: ToolCall,
        result: ToolResult,
    ) -> SupervisionDecision:
        if result.status is ToolResultStatus.SUCCESS:
            context.supervision_data[self._state_key] = 0
            return await super().after_tool(context, call, result)

        count = int(context.supervision_data.get(self._state_key, 0)) + 1
        context.supervision_data[self._state_key] = count
        if count >= context.profile.max_consecutive_tool_failures:
            return SupervisionDecision.redirect(
                self.name,
                reason=f"consecutive tool failures detected ({count})",
                message=(
                    "Multiple tool attempts have failed. Review the error observations, "
                    "change the approach, and avoid issuing another equivalent call."
                ),
                metadata={"count": count, "last_tool": call.name},
            )
        return await super().after_tool(context, call, result)


def build_default_supervisor(profile: AgentProfile) -> CompositeSupervisor:
    del profile  # Policy limits are read from each RuntimeContext at hook time.
    return CompositeSupervisor(
        [
            ExecutionBudget(),
            DuplicateToolCallDetector(),
            NoProgressDetector(),
        ]
    )
