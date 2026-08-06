"""Definition resolution and conservative recovery decisions for Flow work."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from base_agent.flows.lease import FlowExecutionLease
from base_agent.flows.lifecycle import FlowRunState
from base_agent.flows.models import FlowDefinition
from base_agent.flows.repository import FlowRepository
from base_agent.flows.service import FlowLifecycle
from base_agent.flows.side_effects import (
    FlowSideEffect,
    FlowSideEffectEvidenceReader,
)
from base_agent.flows.work import (
    FlowWorkBlockedError,
    FlowWorkCommand,
    FlowWorkItem,
    FlowWorkKind,
)
from base_agent.models import RunStatus, ToolConfirmation

_TERMINAL_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.INTERRUPTED,
    RunStatus.LIMIT_REACHED,
}


class FlowDefinitionNotFoundError(KeyError):
    """Raised when an exact Flow definition version cannot be resolved."""


class FlowDefinitionConflictError(RuntimeError):
    """Raised when one Flow definition identity has conflicting contents."""


class FlowDefinitionMismatchError(RuntimeError):
    """Raised when resolved definition content differs from the pinned Run."""


class FlowDefinitionResolver(Protocol):
    """Resolve an immutable Flow definition by exact ID and version."""

    async def resolve(
        self,
        definition_id: str,
        definition_version: str,
    ) -> FlowDefinition: ...


class InMemoryFlowDefinitionRegistry:
    """Immutable process-local registry for applications and deterministic tests."""

    def __init__(self, definitions: Iterable[FlowDefinition]) -> None:
        self._definitions: dict[tuple[str, str], FlowDefinition] = {}
        for definition in definitions:
            key = (definition.id, definition.version)
            existing = self._definitions.get(key)
            if existing is not None and existing.fingerprint != definition.fingerprint:
                raise FlowDefinitionConflictError(
                    f"Flow definition '{definition.id}@{definition.version}' "
                    f"has conflicting fingerprints"
                )
            self._definitions[key] = definition.model_copy(deep=True)

    async def resolve(
        self,
        definition_id: str,
        definition_version: str,
    ) -> FlowDefinition:
        try:
            definition = self._definitions[(definition_id, definition_version)]
        except KeyError as exc:
            raise FlowDefinitionNotFoundError(
                f"Flow definition '{definition_id}@{definition_version}' "
                f"was not found"
            ) from exc
        return definition.model_copy(deep=True)


class FlowRecoveryAction(StrEnum):
    START = "start"
    ADVANCE = "advance"
    RESUME = "resume"
    FINALIZE = "finalize"
    CANCEL = "cancel"
    NOOP = "noop"
    MANUAL_REVIEW = "manual_review"


class FlowRecoveryReason(StrEnum):
    CREATED_SAFE_TO_START = "created_safe_to_start"
    IDLE_SAFE_TO_ADVANCE = "idle_safe_to_advance"
    SETTLED_FAILURE_SAFE_TO_FINALIZE = "settled_failure_safe_to_finalize"
    WAITING_INPUT_AVAILABLE = "waiting_input_available"
    WAITING_CONFIRMATION_AVAILABLE = "waiting_confirmation_available"
    CANCELLATION_REQUESTED = "cancellation_requested"
    TERMINAL_ALREADY_SETTLED = "terminal_already_settled"
    ACTIVE_INVOCATION_UNCERTAIN = "active_invocation_uncertain"
    ACTIVE_SIDE_EFFECT_RETRY_UNSAFE = "active_side_effect_retry_unsafe"
    WAITING_INPUT_REQUIRED = "waiting_input_required"
    RESUME_STATE_MISMATCH = "resume_state_mismatch"
    UNSUPPORTED_STATE = "unsupported_state"
    DEFINITION_NOT_FOUND = "definition_not_found"
    DEFINITION_FINGERPRINT_MISMATCH = "definition_fingerprint_mismatch"


class FlowRecoveryDecision(BaseModel):
    """One metadata-only decision made before application recovery code runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: FlowRecoveryAction
    reason: FlowRecoveryReason


class FlowRecoveryPolicy:
    """Automatically continue only at boundaries with known side-effect safety."""

    def decide(
        self,
        command: FlowWorkCommand,
        state: FlowRunState,
        *,
        side_effects: tuple[FlowSideEffect, ...] = (),
    ) -> FlowRecoveryDecision:
        if command.run_id != state.run_id:
            raise ValueError("Flow work command targets another Flow Run")
        if state.status in _TERMINAL_STATUSES:
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.NOOP,
                reason=FlowRecoveryReason.TERMINAL_ALREADY_SETTLED,
            )
        if command.kind is FlowWorkKind.CANCEL:
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.CANCEL,
                reason=FlowRecoveryReason.CANCELLATION_REQUESTED,
            )
        if command.kind is FlowWorkKind.RESUME:
            user_input = command.data.get("user_input")
            confirmation = _confirmation_from_command(command)
            has_user_input = isinstance(user_input, str) and bool(
                user_input.strip()
            )
            has_confirmation = confirmation is not None
            if (
                state.status is RunStatus.WAITING
                and has_user_input != has_confirmation
            ):
                return FlowRecoveryDecision(
                    action=FlowRecoveryAction.RESUME,
                    reason=(
                        FlowRecoveryReason.WAITING_CONFIRMATION_AVAILABLE
                        if has_confirmation
                        else FlowRecoveryReason.WAITING_INPUT_AVAILABLE
                    ),
                )
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.MANUAL_REVIEW,
                reason=(
                    FlowRecoveryReason.WAITING_INPUT_REQUIRED
                    if state.status is RunStatus.WAITING
                    else FlowRecoveryReason.RESUME_STATE_MISMATCH
                ),
            )
        if command.kind not in {FlowWorkKind.EXECUTE, FlowWorkKind.RECOVER}:
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.MANUAL_REVIEW,
                reason=FlowRecoveryReason.UNSUPPORTED_STATE,
            )
        if state.status is RunStatus.CREATED:
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.START,
                reason=FlowRecoveryReason.CREATED_SAFE_TO_START,
            )
        if state.status is RunStatus.WAITING:
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.MANUAL_REVIEW,
                reason=FlowRecoveryReason.WAITING_INPUT_REQUIRED,
            )
        if state.status is RunStatus.RUNNING:
            if state.active_invocation is not None:
                return FlowRecoveryDecision(
                    action=FlowRecoveryAction.MANUAL_REVIEW,
                    reason=(
                        FlowRecoveryReason.ACTIVE_SIDE_EFFECT_RETRY_UNSAFE
                        if side_effects
                        and any(not effect.retry_safe for effect in side_effects)
                        else FlowRecoveryReason.ACTIVE_INVOCATION_UNCERTAIN
                    ),
                )
            if (
                not state.invocations
                or state.invocations[-1].status is RunStatus.COMPLETED
            ):
                return FlowRecoveryDecision(
                    action=FlowRecoveryAction.ADVANCE,
                    reason=FlowRecoveryReason.IDLE_SAFE_TO_ADVANCE,
                )
            return FlowRecoveryDecision(
                action=FlowRecoveryAction.FINALIZE,
                reason=FlowRecoveryReason.SETTLED_FAILURE_SAFE_TO_FINALIZE,
            )
        return FlowRecoveryDecision(
            action=FlowRecoveryAction.MANUAL_REVIEW,
            reason=FlowRecoveryReason.UNSUPPORTED_STATE,
        )


@dataclass(frozen=True, slots=True)
class FlowRecoveryContext:
    item: FlowWorkItem
    definition: FlowDefinition
    state: FlowRunState
    decision: FlowRecoveryDecision
    lifecycle: FlowLifecycle
    side_effects: tuple[FlowSideEffect, ...] = ()


class FlowRecoveryDispatcher(Protocol):
    """Application strategy that executes one already-validated recovery decision."""

    async def __call__(self, context: FlowRecoveryContext) -> None: ...


class DefinitionResolvingFlowWorkHandler:
    """Resolve pinned definitions and gate work through a recovery policy."""

    def __init__(
        self,
        repository: FlowRepository,
        resolver: FlowDefinitionResolver,
        dispatcher: FlowRecoveryDispatcher,
        *,
        policy: FlowRecoveryPolicy | None = None,
        side_effect_evidence: FlowSideEffectEvidenceReader | None = None,
    ) -> None:
        self._repository = repository
        self._resolver = resolver
        self._dispatcher = dispatcher
        self._policy = policy or FlowRecoveryPolicy()
        self._side_effect_evidence = side_effect_evidence

    async def __call__(
        self,
        item: FlowWorkItem,
        lease: FlowExecutionLease,
    ) -> None:
        state = await self._repository.get(item.command.run_id)
        try:
            definition = await self._resolver.resolve(
                state.definition_id,
                state.definition_version,
            )
        except FlowDefinitionNotFoundError as exc:
            raise FlowWorkBlockedError(
                FlowRecoveryReason.DEFINITION_NOT_FOUND.value
            ) from exc
        try:
            _validate_resolved_definition(definition, state)
        except FlowDefinitionMismatchError as exc:
            raise FlowWorkBlockedError(
                FlowRecoveryReason.DEFINITION_FINGERPRINT_MISMATCH.value
            ) from exc
        side_effects: tuple[FlowSideEffect, ...] = ()
        if (
            state.active_invocation is not None
            and self._side_effect_evidence is not None
        ):
            side_effects = await self._side_effect_evidence.list_for_invocation(
                state.active_invocation.id
            )
        decision = self._policy.decide(
            item.command,
            state,
            side_effects=side_effects,
        )
        if decision.action is FlowRecoveryAction.MANUAL_REVIEW:
            raise FlowWorkBlockedError(decision.reason.value)
        if decision.action is FlowRecoveryAction.NOOP:
            return
        await self._dispatcher(
            FlowRecoveryContext(
                item=item,
                definition=definition,
                state=state,
                decision=decision,
                lifecycle=FlowLifecycle(
                    self._repository,
                    execution_lease=lease,
                ),
                side_effects=side_effects,
            )
        )


_definition_resolver_contract: type[FlowDefinitionResolver] = (
    InMemoryFlowDefinitionRegistry
)


def _validate_resolved_definition(
    definition: FlowDefinition,
    state: FlowRunState,
) -> None:
    if (
        definition.id != state.definition_id
        or definition.version != state.definition_version
        or definition.fingerprint != state.definition_fingerprint
    ):
        raise FlowDefinitionMismatchError(
            f"resolved Flow definition does not match Run '{state.run_id}'"
        )


def _confirmation_from_command(
    command: FlowWorkCommand,
) -> ToolConfirmation | None:
    payload = command.data.get("confirmation")
    if not isinstance(payload, dict):
        return None
    try:
        return ToolConfirmation.model_validate(payload)
    except ValueError:
        return None
