"""Fail-closed confirmation contracts for side-effecting Tool calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class SideEffect(StrEnum):
    READ_ONLY = "read_only"
    IDEMPOTENT = "idempotent"
    MUTATING = "mutating"


class Confirmation(StrEnum):
    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    run_id: str
    agent_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    side_effect: SideEffect


@runtime_checkable
class ConfirmationPolicy(Protocol):
    async def confirm(self, request: ConfirmationRequest) -> bool: ...


class DenyConfirmations:
    async def confirm(self, request: ConfirmationRequest) -> bool:
        return False


class AllowAllConfirmations:
    """Test helper; production applications should ask an authenticated user."""

    async def confirm(self, request: ConfirmationRequest) -> bool:
        return True
