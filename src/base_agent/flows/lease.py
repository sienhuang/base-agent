"""Execution ownership contracts for durable Flow workers."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from base_agent.models.run import utc_now


class FlowLeaseUnavailableError(RuntimeError):
    """Raised when another worker still owns a Flow execution lease."""


class FlowLeaseLostError(RuntimeError):
    """Raised when a worker uses an expired or superseded fencing token."""


class FlowExecutionLease(BaseModel):
    """One time-bounded, fenced right to advance a Flow Run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: UUID
    token: UUID
    owner_id: str = Field(min_length=1, max_length=256)
    attempt: int = Field(ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return self.expires_at <= (now or utc_now())


class FlowLeaseRepository(Protocol):
    """Claim, renew, and release durable Flow execution ownership."""

    async def acquire_execution(
        self,
        run_id: UUID,
        *,
        owner_id: str,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowExecutionLease: ...

    async def renew_execution(
        self,
        lease: FlowExecutionLease,
        *,
        ttl_seconds: float,
        now: datetime | None = None,
    ) -> FlowExecutionLease: ...

    async def release_execution(self, lease: FlowExecutionLease) -> None: ...
