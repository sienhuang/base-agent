"""Metadata-only evidence for recovering interrupted external side effects."""

from __future__ import annotations

import asyncio
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.models.run import utc_now

_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$"


class FlowSideEffectNotFoundError(KeyError):
    """Raised when a side-effect record does not exist."""


class FlowSideEffectConflictError(RuntimeError):
    """Raised when an operation key is reused with different intent."""


class FlowSideEffectRevisionConflictError(RuntimeError):
    """Raised when a stale writer attempts a side-effect transition."""


class InvalidFlowSideEffectTransitionError(RuntimeError):
    """Raised when evidence is moved through an unsafe lifecycle transition."""


class FlowSideEffectPhase(StrEnum):
    """Durable knowledge about one external side effect."""

    PREPARED = "prepared"
    STARTED = "started"
    CONFIRMED = "confirmed"
    ABORTED = "aborted"


class FlowSideEffectRetryMode(StrEnum):
    """Whether replay must rely on downstream idempotency."""

    UNSAFE = "unsafe"
    IDEMPOTENT = "idempotent"


class FlowSideEffect(BaseModel):
    """One bounded record; arguments, results, and exception text are excluded."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID = Field(default_factory=uuid4)
    flow_run_id: UUID
    invocation_id: UUID
    operation_key: str = Field(
        min_length=1,
        max_length=256,
        pattern=_KEY_PATTERN,
    )
    operation_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=_KEY_PATTERN,
    )
    retry_mode: FlowSideEffectRetryMode = FlowSideEffectRetryMode.UNSAFE
    idempotency_key_digest: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    phase: FlowSideEffectPhase = FlowSideEffectPhase.PREPARED
    revision: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_retry_evidence(self) -> FlowSideEffect:
        if (
            self.retry_mode is FlowSideEffectRetryMode.IDEMPOTENT
            and self.idempotency_key_digest is None
        ):
            raise ValueError(
                "idempotent side effects require an idempotency key digest"
            )
        if (
            self.retry_mode is FlowSideEffectRetryMode.UNSAFE
            and self.idempotency_key_digest is not None
        ):
            raise ValueError(
                "unsafe side effects cannot claim an idempotency key digest"
            )
        return self

    @property
    def retry_safe(self) -> bool:
        """Whether replay is safe based only on persisted evidence."""
        return (
            self.phase in {
                FlowSideEffectPhase.PREPARED,
                FlowSideEffectPhase.ABORTED,
            }
            or self.retry_mode is FlowSideEffectRetryMode.IDEMPOTENT
        )

    def has_same_intent(self, other: FlowSideEffect) -> bool:
        return (
            self.flow_run_id == other.flow_run_id
            and self.invocation_id == other.invocation_id
            and self.operation_key == other.operation_key
            and self.operation_name == other.operation_name
            and self.retry_mode is other.retry_mode
            and self.idempotency_key_digest == other.idempotency_key_digest
        )


class FlowSideEffectEvidenceReader(Protocol):
    """Read-only recovery view, intentionally narrower than the writer port."""

    async def list_for_invocation(
        self,
        invocation_id: UUID,
    ) -> tuple[FlowSideEffect, ...]: ...


class FlowSideEffectLedger(FlowSideEffectEvidenceReader, Protocol):
    """Persist intent before transport and fenced lifecycle evidence around it."""

    async def prepare(self, effect: FlowSideEffect) -> FlowSideEffect: ...

    async def get(self, effect_id: UUID) -> FlowSideEffect: ...

    async def mark_started(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect: ...

    async def confirm(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect: ...

    async def abort(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect: ...


class InMemoryFlowSideEffectLedger:
    """Process-local ledger with the same idempotency and CAS contract as adapters."""

    def __init__(self) -> None:
        self._effects: dict[UUID, FlowSideEffect] = {}
        self._operations: dict[tuple[UUID, UUID, str], UUID] = {}
        self._lock = asyncio.Lock()

    async def prepare(self, effect: FlowSideEffect) -> FlowSideEffect:
        if effect.phase is not FlowSideEffectPhase.PREPARED or effect.revision != 1:
            raise ValueError("new side-effect evidence must be prepared at revision 1")
        operation = (
            effect.flow_run_id,
            effect.invocation_id,
            effect.operation_key,
        )
        async with self._lock:
            existing_id = self._operations.get(operation)
            if existing_id is not None:
                existing = self._effects[existing_id]
                if not existing.has_same_intent(effect):
                    raise FlowSideEffectConflictError(
                        f"side-effect operation '{effect.operation_key}' "
                        "has different intent"
                    )
                return existing.model_copy(deep=True)
            stored = effect.model_copy(deep=True)
            self._effects[stored.id] = stored
            self._operations[operation] = stored.id
            return stored.model_copy(deep=True)

    async def get(self, effect_id: UUID) -> FlowSideEffect:
        async with self._lock:
            try:
                effect = self._effects[effect_id]
            except KeyError as exc:
                raise FlowSideEffectNotFoundError(
                    f"side-effect evidence '{effect_id}' was not found"
                ) from exc
            return effect.model_copy(deep=True)

    async def list_for_invocation(
        self,
        invocation_id: UUID,
    ) -> tuple[FlowSideEffect, ...]:
        async with self._lock:
            matches = sorted(
                (
                    effect
                    for effect in self._effects.values()
                    if effect.invocation_id == invocation_id
                ),
                key=lambda effect: (effect.created_at, str(effect.id)),
            )
            return tuple(effect.model_copy(deep=True) for effect in matches)

    async def mark_started(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect:
        return await self._transition(
            effect_id,
            expected_revision=expected_revision,
            target=FlowSideEffectPhase.STARTED,
        )

    async def confirm(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect:
        return await self._transition(
            effect_id,
            expected_revision=expected_revision,
            target=FlowSideEffectPhase.CONFIRMED,
        )

    async def abort(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
    ) -> FlowSideEffect:
        return await self._transition(
            effect_id,
            expected_revision=expected_revision,
            target=FlowSideEffectPhase.ABORTED,
        )

    async def _transition(
        self,
        effect_id: UUID,
        *,
        expected_revision: int,
        target: FlowSideEffectPhase,
    ) -> FlowSideEffect:
        async with self._lock:
            try:
                current = self._effects[effect_id]
            except KeyError as exc:
                raise FlowSideEffectNotFoundError(
                    f"side-effect evidence '{effect_id}' was not found"
                ) from exc
            if current.revision != expected_revision:
                raise FlowSideEffectRevisionConflictError(
                    f"side-effect evidence '{effect_id}' expected revision "
                    f"{expected_revision}, found {current.revision}"
                )
            _validate_transition(current.phase, target)
            replacement = current.model_copy(
                update={
                    "phase": target,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            self._effects[effect_id] = replacement
            return replacement.model_copy(deep=True)


def _validate_transition(
    current: FlowSideEffectPhase,
    target: FlowSideEffectPhase,
) -> None:
    valid = {
        FlowSideEffectPhase.PREPARED: {
            FlowSideEffectPhase.STARTED,
            FlowSideEffectPhase.ABORTED,
        },
        FlowSideEffectPhase.STARTED: {FlowSideEffectPhase.CONFIRMED},
        FlowSideEffectPhase.CONFIRMED: set(),
        FlowSideEffectPhase.ABORTED: {FlowSideEffectPhase.STARTED},
    }
    if target not in valid[current]:
        raise InvalidFlowSideEffectTransitionError(
            f"cannot move side-effect evidence from '{current.value}' "
            f"to '{target.value}'"
        )


_ledger_contract: type[FlowSideEffectLedger] = InMemoryFlowSideEffectLedger
