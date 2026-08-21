"""Lifecycle status shared by Flow strategies and durable control state."""

from enum import StrEnum


class FlowStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    RECOVERY_REQUIRED = "recovery_required"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


__all__ = ["FlowStatus"]
