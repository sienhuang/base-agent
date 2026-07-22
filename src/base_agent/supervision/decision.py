"""Structured decisions returned by Supervisor policies."""

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.models import RunStatus


class SupervisionAction(StrEnum):
    CONTINUE = "continue"
    REDIRECT = "redirect"
    STOP = "stop"


class SupervisionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: str = Field(min_length=1)
    action: SupervisionAction
    reason: str | None = None
    message: str | None = None
    terminal_status: RunStatus | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        if self.action is SupervisionAction.CONTINUE:
            if self.terminal_status is not None:
                raise ValueError("continue decisions cannot set terminal_status")
            return self
        if not self.reason:
            raise ValueError("supervisor interventions require a reason")
        if self.action is SupervisionAction.REDIRECT:
            if not self.message:
                raise ValueError("redirect decisions require a message")
            if self.terminal_status is not None:
                raise ValueError("redirect decisions cannot set terminal_status")
        elif self.terminal_status not in {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.LIMIT_REACHED,
        }:
            raise ValueError("stop decisions require a terminal_status")
        return self

    @classmethod
    def continue_(cls, policy: str) -> "SupervisionDecision":
        return cls(policy=policy, action=SupervisionAction.CONTINUE)

    @classmethod
    def redirect(
        cls,
        policy: str,
        *,
        reason: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> "SupervisionDecision":
        return cls(
            policy=policy,
            action=SupervisionAction.REDIRECT,
            reason=reason,
            message=message,
            metadata=metadata or {},
        )

    @classmethod
    def stop(
        cls,
        policy: str,
        *,
        reason: str,
        terminal_status: RunStatus = RunStatus.LIMIT_REACHED,
        metadata: dict[str, Any] | None = None,
    ) -> "SupervisionDecision":
        return cls(
            policy=policy,
            action=SupervisionAction.STOP,
            reason=reason,
            terminal_status=terminal_status,
            metadata=metadata or {},
        )
