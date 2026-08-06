"""Adapt Tool execution governance onto the Flow side-effect ledger."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from base_agent.flows.side_effects import (
    FlowSideEffect,
    FlowSideEffectLedger,
    FlowSideEffectPhase,
    FlowSideEffectRetryMode,
    FlowSideEffectRevisionConflictError,
)
from base_agent.models import ToolCall
from base_agent.tools import (
    ToolContext,
    ToolSideEffectContextError,
    ToolSideEffectMode,
    ToolSideEffectReceipt,
    ToolSideEffectRecorder,
    ToolSideEffectRecorderError,
    ToolSideEffectReplayUnsafeError,
)


@dataclass(frozen=True, slots=True)
class _FlowEffectReceipt:
    effect_id: UUID
    expected_revision: int
    confirmation_required: bool


class FlowToolSideEffectRecorder:
    """Record governed Tool transports using Flow/Invocation correlation."""

    def __init__(self, ledger: FlowSideEffectLedger) -> None:
        self._ledger = ledger

    async def start(
        self,
        call: ToolCall,
        *,
        tool_name: str,
        mode: ToolSideEffectMode,
        context: ToolContext,
    ) -> ToolSideEffectReceipt:
        if context.flow_run_id is None or context.invocation_id is None:
            raise ToolSideEffectContextError(
                "Flow Tool evidence requires Flow Run and Invocation IDs"
            )
        if mode not in {
            ToolSideEffectMode.UNSAFE,
            ToolSideEffectMode.IDEMPOTENT,
        }:
            raise ToolSideEffectRecorderError(
                "only side-effecting Tools require Flow evidence"
            )
        idempotency_key = (
            _idempotency_key(context.flow_run_id, context.invocation_id, call.id)
            if mode is ToolSideEffectMode.IDEMPOTENT
            else None
        )
        effect = await self._ledger.prepare(
            FlowSideEffect(
                flow_run_id=context.flow_run_id,
                invocation_id=context.invocation_id,
                operation_key=_operation_key(call.id),
                operation_name=tool_name,
                retry_mode=(
                    FlowSideEffectRetryMode.IDEMPOTENT
                    if mode is ToolSideEffectMode.IDEMPOTENT
                    else FlowSideEffectRetryMode.UNSAFE
                ),
                idempotency_key_digest=(
                    _digest(idempotency_key)
                    if idempotency_key is not None
                    else None
                ),
            )
        )
        started = await self._start_or_replay(effect)
        return ToolSideEffectReceipt(
            token=_FlowEffectReceipt(
                effect_id=started.id,
                expected_revision=started.revision,
                confirmation_required=(
                    started.phase is not FlowSideEffectPhase.CONFIRMED
                ),
            ),
            idempotency_key=idempotency_key,
        )

    async def confirm(self, receipt: ToolSideEffectReceipt) -> None:
        token = receipt.token
        if not isinstance(token, _FlowEffectReceipt):
            raise ToolSideEffectRecorderError(
                "side-effect receipt belongs to another recorder"
            )
        if not token.confirmation_required:
            return
        try:
            await self._ledger.confirm(
                token.effect_id,
                expected_revision=token.expected_revision,
            )
        except FlowSideEffectRevisionConflictError as exc:
            current = await self._ledger.get(token.effect_id)
            if current.phase is FlowSideEffectPhase.CONFIRMED:
                return
            raise ToolSideEffectRecorderError(
                "side-effect confirmation lost revision ownership"
            ) from exc

    async def _start_or_replay(
        self,
        effect: FlowSideEffect,
    ) -> FlowSideEffect:
        while effect.phase in {
            FlowSideEffectPhase.PREPARED,
            FlowSideEffectPhase.ABORTED,
        }:
            try:
                return await self._ledger.mark_started(
                    effect.id,
                    expected_revision=effect.revision,
                )
            except FlowSideEffectRevisionConflictError:
                effect = await self._ledger.get(effect.id)
        if effect.retry_mode is FlowSideEffectRetryMode.IDEMPOTENT:
            return effect
        raise ToolSideEffectReplayUnsafeError(
            "unprotected side effect may already have executed"
        )


def _operation_key(tool_call_id: str) -> str:
    return f"tool:{_digest(tool_call_id)}"


def _idempotency_key(
    flow_run_id: UUID,
    invocation_id: UUID,
    tool_call_id: str,
) -> str:
    return (
        f"base-agent-v1:{flow_run_id}:{invocation_id}:"
        f"{_digest(tool_call_id)}"
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


_recorder_contract: type[ToolSideEffectRecorder] = FlowToolSideEffectRecorder
