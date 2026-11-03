"""Built-in ContextPolicy implementations."""

from __future__ import annotations

from dataclasses import dataclass

from dsh_base_agent.context.models import (
    ContextDecision,
    ContextPolicyContext,
    ContextSelection,
    VisibilityScope,
)
from dsh_base_agent.context.protocol import ContextPolicy
from dsh_base_agent.memory import RecallQuery


@dataclass(frozen=True, slots=True)
class KeepAllContextPolicy:
    name: str = "keep-all"
    version: str = "1"

    async def decide(self, context: ContextPolicyContext) -> ContextDecision:
        return ContextDecision(
            history_event_sequences=tuple(item.event_sequence for item in context.history),
            context_blocks=tuple(
                ContextSelection(ref=item.id, scope=VisibilityScope.TURN)
                for item in context.context_blocks
            ),
            reason_code="keep_all",
            estimated_tokens=(
                sum(item.token_estimate for item in context.history)
                + sum(item.token_estimate for item in context.context_blocks)
            ),
        )


@dataclass(frozen=True, slots=True)
class CompactingContextPolicy:
    """Compile to DSH compaction-basic; do not compact transcripts in Python."""

    threshold_ratio: float = 0.8
    retain_ratio: float = 0.16
    name: str = "dsh-compaction-basic"
    version: str = "1"

    def __post_init__(self) -> None:
        if not 0 < self.threshold_ratio <= 1:
            raise ValueError("threshold_ratio must be in (0, 1]")
        if not 0 < self.retain_ratio < self.threshold_ratio:
            raise ValueError("retain_ratio must be in (0, threshold_ratio)")

    async def decide(self, context: ContextPolicyContext) -> ContextDecision:
        return ContextDecision(
            history_event_sequences=tuple(item.event_sequence for item in context.history),
            context_blocks=tuple(
                ContextSelection(ref=item.id, scope=VisibilityScope.TURN)
                for item in context.context_blocks
            ),
            reason_code="native_compaction",
            estimated_tokens=(
                sum(item.token_estimate for item in context.history)
                + sum(item.token_estimate for item in context.context_blocks)
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoryAugmentedContextPolicy:
    """Recall Memory only when this composed policy is explicitly selected."""

    base: ContextPolicy
    minimum_score: float = 0.75
    limit: int = 8
    scope: VisibilityScope = VisibilityScope.REQUEST
    name: str = "memory-augmented"
    version: str = "1"

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_score <= 1:
            raise ValueError("minimum_score must be in [0, 1]")
        if self.limit < 1:
            raise ValueError("limit must be at least 1")

    async def decide(self, context: ContextPolicyContext) -> ContextDecision:
        base_decision = await self.base.decide(context)
        candidates = await context.memory.recall(
            RecallQuery(
                tenant=context.tenant,
                namespace=context.namespace,
                text=context.current_input,
                limit=self.limit,
            )
        )
        selected = tuple(
            ContextSelection(ref=item.ref, scope=self.scope)
            for item in candidates
            if item.score >= self.minimum_score
        )
        selected_refs = {item.ref for item in selected}
        memory_tokens = sum(
            max(1, len(item.content) // 4) for item in candidates if item.ref in selected_refs
        )
        return base_decision.model_copy(
            update={
                "memories": selected,
                "reason_code": "memory_augmented",
                "estimated_tokens": base_decision.estimated_tokens + memory_tokens,
            }
        )


def native_context_configuration(policy: ContextPolicy) -> dict[str, object] | None:
    """Return the DSH-native portion of a Policy, if it has one."""

    if isinstance(policy, KeepAllContextPolicy):
        return {"mode": "keep-all"}
    if isinstance(policy, CompactingContextPolicy):
        return {
            "mode": "compaction-basic",
            "thresholdRatio": policy.threshold_ratio,
            "retainRatio": policy.retain_ratio,
        }
    if isinstance(policy, MemoryAugmentedContextPolicy):
        base = native_context_configuration(policy.base)
        return None if base is None else {**base, "requiresContextGateway": True}
    return None
