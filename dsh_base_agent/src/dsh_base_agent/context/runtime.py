"""Fail-closed ContextPolicy invocation and decision validation."""

from __future__ import annotations

import asyncio
import hashlib

from dsh_base_agent.context.models import (
    CommittedContextDecision,
    ContextDecision,
    ContextPolicyContext,
)
from dsh_base_agent.context.protocol import ContextPolicy
from dsh_base_agent.events import EventStore


class ContextDecisionError(RuntimeError):
    pass


class ContextRuntime:
    def __init__(
        self,
        event_store: EventStore,
        *,
        timeout_seconds: float = 2.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._events = event_store
        self._timeout_seconds = timeout_seconds

    async def decide(
        self,
        policy: ContextPolicy,
        context: ContextPolicyContext,
    ) -> CommittedContextDecision:
        await self._events.append(
            context.run_id,
            "context.decision.started",
            {
                "policy_name": policy.name,
                "policy_version": policy.version,
                "input_revision": context.input_revision,
                "turn": context.turn,
                "step": context.step,
            },
        )
        try:
            decision = await asyncio.wait_for(
                policy.decide(context),
                timeout=self._timeout_seconds,
            )
            _validate_decision(decision, context)
            digest = hashlib.sha256(
                decision.model_dump_json(exclude_none=False).encode()
            ).hexdigest()
            committed = CommittedContextDecision(
                policy_name=policy.name,
                policy_version=policy.version,
                input_revision=context.input_revision,
                digest=digest,
                decision=decision,
            )
            await self._events.append(
                context.run_id,
                "context.decision.committed",
                {
                    "policy_name": policy.name,
                    "policy_version": policy.version,
                    "input_revision": context.input_revision,
                    "digest": digest,
                    "history_event_sequences": list(decision.history_event_sequences),
                    "context_refs": [item.ref for item in decision.context_blocks],
                    "memory_refs": [item.ref for item in decision.memories],
                },
            )
            return committed
        except BaseException as exc:
            await self._events.append(
                context.run_id,
                "context.decision.failed",
                {
                    "policy_name": policy.name,
                    "input_revision": context.input_revision,
                    "error_type": type(exc).__name__,
                },
            )
            raise


def _validate_decision(
    decision: ContextDecision,
    context: ContextPolicyContext,
) -> None:
    available_history = {item.event_sequence for item in context.history}
    if len(set(decision.history_event_sequences)) != len(decision.history_event_sequences):
        raise ContextDecisionError("history selection contains duplicate event sequences")
    if not set(decision.history_event_sequences) <= available_history:
        raise ContextDecisionError("history selection references unavailable events")
    available_blocks = {item.id for item in context.context_blocks}
    selected_blocks = [item.ref for item in decision.context_blocks]
    if len(set(selected_blocks)) != len(selected_blocks):
        raise ContextDecisionError("context block selection contains duplicate refs")
    if not set(selected_blocks) <= available_blocks:
        raise ContextDecisionError("context selection references unavailable blocks")
    memory_refs = [item.ref for item in decision.memories]
    if len(set(memory_refs)) != len(memory_refs):
        raise ContextDecisionError("Memory selection contains duplicate refs")
    if not set(memory_refs) <= context.memory.recalled_refs:
        raise ContextDecisionError("Memory selection references candidates not recalled")
    if decision.estimated_tokens > context.available_tokens:
        raise ContextDecisionError("ContextDecision exceeds the available token budget")
