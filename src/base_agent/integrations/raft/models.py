"""Models and configuration for the Raft External Agent adapter."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

_SAFE_ADAPTER_INSTANCE = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class RaftMessage:
    """One canonical message drained from the External Agent inbox."""

    target: str
    message_id: str
    timestamp: str
    sender_type: str
    sender_handle: str
    content: str
    task_number: int | None = None
    task_status: str | None = None
    task_assignee_type: str | None = None
    task_assignee_id: str | None = None

    @property
    def is_direct_message(self) -> bool:
        return self.target.startswith("dm:@")

    @property
    def is_thread(self) -> bool:
        if self.target.startswith("dm:@"):
            return self.target.count(":") >= 2
        return self.target.startswith("#") and ":" in self.target


@dataclass(frozen=True, slots=True)
class RaftInboxBatch:
    """Raw CLI output plus the messages parsed from it."""

    raw: str
    messages: tuple[RaftMessage, ...]


@dataclass(frozen=True, slots=True)
class RaftWorkerConfig:
    """Runtime settings for one application-owned Raft External Agent."""

    profile: str
    agent_id: UUID
    handle: str
    executable: str = "raft"
    state_dir: Path = field(default_factory=lambda: Path(".base-agent/raft"))
    adapter_instance: str = "base-agent"
    cli_timeout_seconds: float = 30.0
    cli_max_output_bytes: int = 4 * 1024 * 1024
    bridge_poll_interval_ms: int = 5_000
    max_reply_chars: int = 8_000
    processed_message_limit: int = 5_000

    def __post_init__(self) -> None:
        profile = self.profile.strip()
        handle = self.handle.strip().removeprefix("@")
        executable = self.executable.strip()
        adapter_instance = self.adapter_instance.strip()
        if not profile:
            raise ValueError("Raft profile must not be blank")
        if not handle or any(character.isspace() for character in handle):
            raise ValueError("Raft handle must be a non-blank handle without spaces")
        if not executable:
            raise ValueError("Raft executable must not be blank")
        if not _SAFE_ADAPTER_INSTANCE.fullmatch(adapter_instance):
            raise ValueError(
                "Raft adapter_instance may contain only letters, numbers, '.', '_', and '-'"
            )
        if self.cli_timeout_seconds <= 0:
            raise ValueError("Raft CLI timeout must be greater than zero")
        if self.cli_max_output_bytes < 1:
            raise ValueError("Raft CLI output limit must be greater than zero")
        if self.bridge_poll_interval_ms < 250:
            raise ValueError("Raft bridge poll interval must be at least 250ms")
        if self.max_reply_chars < 256:
            raise ValueError("Raft maximum reply size must be at least 256 characters")
        if self.processed_message_limit < 100:
            raise ValueError("Raft processed message limit must be at least 100")
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "handle", handle)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "adapter_instance", adapter_instance)
        object.__setattr__(self, "state_dir", Path(self.state_dir))

    @property
    def profile_state_dir(self) -> Path:
        safe_profile = re.sub(r"[^A-Za-z0-9._-]", "_", self.profile)[:120]
        return self.state_dir / (safe_profile or "default")


@dataclass(frozen=True, slots=True)
class RaftDrainResult:
    """Summary of one inbox drain without including message contents."""

    received: int
    handled: int
    skipped: int
