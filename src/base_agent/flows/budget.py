"""Flow-wide budget measurement and enforcement decisions."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from base_agent.flows.lifecycle import FlowRunState


class FlowBudgetKind(StrEnum):
    INVOCATIONS = "invocations"
    INPUT_TOKENS = "input_tokens"
    OUTPUT_TOKENS = "output_tokens"
    TOTAL_TOKENS = "total_tokens"
    MODEL_CALLS = "model_calls"
    TOOL_CALLS = "tool_calls"
    TIMEOUT_SECONDS = "timeout_seconds"


class FlowConsumption(BaseModel):
    """Current aggregate consumption derived from persisted invocation results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocations: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)


class FlowBudgetExceeded(BaseModel):
    """Structured reason used to terminate a Flow at its budget boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: FlowBudgetKind
    limit: float
    actual: float

    @property
    def message(self) -> str:
        return (
            f"Flow budget '{self.kind.value}' reached: "
            f"limit={self.limit:g}, actual={self.actual:g}"
        )


class FlowBudgetPolicy:
    """Evaluate persisted aggregate usage before and after Agent transport."""

    def consumption(
        self,
        state: FlowRunState,
        *,
        now: datetime,
    ) -> FlowConsumption:
        return FlowConsumption(
            invocations=len(state.invocations),
            input_tokens=state.usage.input_tokens,
            output_tokens=state.usage.output_tokens,
            total_tokens=state.usage.total_tokens,
            model_calls=state.model_call_count,
            tool_calls=state.tool_call_count,
            elapsed_seconds=max(0.0, (now - state.created_at).total_seconds()),
        )

    def before_transport(
        self,
        state: FlowRunState,
        *,
        now: datetime,
        new_invocation: bool,
    ) -> FlowBudgetExceeded | None:
        consumption = self.consumption(state, now=now)
        if state.deadline_at is not None and now >= state.deadline_at:
            return self._timeout_violation(state, consumption)
        if (
            new_invocation
            and consumption.invocations >= state.budget.max_invocations
        ):
            return FlowBudgetExceeded(
                kind=FlowBudgetKind.INVOCATIONS,
                limit=state.budget.max_invocations,
                actual=consumption.invocations,
            )
        return self._cumulative_violation(
            state,
            consumption,
            inclusive=True,
        )

    def after_transport(
        self,
        state: FlowRunState,
        *,
        now: datetime,
    ) -> FlowBudgetExceeded | None:
        consumption = self.consumption(state, now=now)
        if state.deadline_at is not None and now >= state.deadline_at:
            return self._timeout_violation(state, consumption)
        return self._cumulative_violation(
            state,
            consumption,
            inclusive=False,
        )

    @staticmethod
    def remaining_seconds(
        state: FlowRunState,
        *,
        now: datetime,
    ) -> float | None:
        if state.deadline_at is None:
            return None
        return max(0.0, (state.deadline_at - now).total_seconds())

    @staticmethod
    def _timeout_violation(
        state: FlowRunState,
        consumption: FlowConsumption,
    ) -> FlowBudgetExceeded:
        assert state.budget.timeout_seconds is not None
        return FlowBudgetExceeded(
            kind=FlowBudgetKind.TIMEOUT_SECONDS,
            limit=state.budget.timeout_seconds,
            actual=consumption.elapsed_seconds,
        )

    @staticmethod
    def _cumulative_violation(
        state: FlowRunState,
        consumption: FlowConsumption,
        *,
        inclusive: bool,
    ) -> FlowBudgetExceeded | None:
        configured = (
            (
                FlowBudgetKind.INPUT_TOKENS,
                state.budget.max_input_tokens,
                consumption.input_tokens,
            ),
            (
                FlowBudgetKind.OUTPUT_TOKENS,
                state.budget.max_output_tokens,
                consumption.output_tokens,
            ),
            (
                FlowBudgetKind.TOTAL_TOKENS,
                state.budget.max_total_tokens,
                consumption.total_tokens,
            ),
            (
                FlowBudgetKind.MODEL_CALLS,
                state.budget.max_model_calls,
                consumption.model_calls,
            ),
            (
                FlowBudgetKind.TOOL_CALLS,
                state.budget.max_tool_calls,
                consumption.tool_calls,
            ),
        )
        for kind, limit, actual in configured:
            if limit is None:
                continue
            reached = actual >= limit if inclusive else actual > limit
            if reached:
                return FlowBudgetExceeded(
                    kind=kind,
                    limit=limit,
                    actual=actual,
                )
        return None
