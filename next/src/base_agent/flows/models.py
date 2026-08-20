"""Values shared by application-facing Flow implementations."""

from enum import StrEnum


class FlowStatus(StrEnum):
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
