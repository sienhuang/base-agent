"""Normalized outcomes from tool execution."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class ToolResultStatus(StrEnum):
    SUCCESS = "success"
    INVALID_ARGUMENTS = "invalid_arguments"
    NOT_FOUND = "not_found"
    DENIED = "denied"
    TIMEOUT = "timeout"
    ERROR = "error"
    WAITING = "waiting"


class ToolResult(BaseModel):
    """A JSON-serializable result that can safely re-enter model context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    status: ToolResultStatus
    data: Any = None
    error_code: str | None = None
    message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is ToolResultStatus.SUCCESS
