"""Values shared by application-facing Flow implementations."""

from enum import StrEnum


class FlowStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
