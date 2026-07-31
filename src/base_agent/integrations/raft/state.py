"""Private, file-backed recovery journal for a Raft Worker."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base_agent.integrations.raft.errors import RaftStateError


class CachedReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    content: str
    move_task_to_review: bool = False
    task_target: str | None = None
    task_number: int | None = None


class OpenTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    number: int


class WorkerState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    completed_ids: list[str] = Field(default_factory=list)
    claimed_ids: set[str] = Field(default_factory=set)
    replied_ids: set[str] = Field(default_factory=set)
    cached_replies: dict[str, CachedReply] = Field(default_factory=dict)
    waiting_runs: dict[str, str] = Field(default_factory=dict)
    open_tasks: dict[str, OpenTask] = Field(default_factory=dict)


class RaftWorkerStateStore:
    """Persist delivery phases so transport retries do not rerun completed work."""

    def __init__(self, root: Path, *, completed_limit: int) -> None:
        self.root = root
        self.completed_limit = completed_limit
        self.state_path = root / "worker-state.json"
        self.spool_path = root / "pending-inbox.txt"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state = self._load()

    def is_completed(self, message_id: str) -> bool:
        return message_id in self.state.completed_ids

    def save(self) -> None:
        self._write_atomic(
            self.state_path,
            self.state.model_dump_json(indent=2),
        )

    def mark_completed(self, message_id: str) -> None:
        if message_id not in self.state.completed_ids:
            self.state.completed_ids.append(message_id)
            overflow = len(self.state.completed_ids) - self.completed_limit
            if overflow > 0:
                del self.state.completed_ids[:overflow]
        self.state.claimed_ids.discard(message_id)
        self.state.replied_ids.discard(message_id)
        self.state.cached_replies.pop(message_id, None)
        self.save()

    def read_spool(self) -> str | None:
        try:
            return self.spool_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RaftStateError(
                f"failed to read Raft inbox spool '{self.spool_path}'"
            ) from exc

    def write_spool(self, raw: str) -> None:
        self._write_atomic(self.spool_path, raw)

    def clear_spool(self) -> None:
        try:
            self.spool_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise RaftStateError(
                f"failed to remove Raft inbox spool '{self.spool_path}'"
            ) from exc

    def _load(self) -> WorkerState:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return WorkerState()
        except OSError as exc:
            raise RaftStateError(
                f"failed to read Raft Worker state '{self.state_path}'"
            ) from exc
        try:
            return WorkerState.model_validate_json(raw)
        except ValidationError as exc:
            raise RaftStateError(
                f"Raft Worker state '{self.state_path}' is invalid"
            ) from exc

    @staticmethod
    def _write_atomic(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise RaftStateError(f"failed to persist Raft state '{path}'") from exc
