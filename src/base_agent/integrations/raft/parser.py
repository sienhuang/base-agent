"""Parser for the canonical human-readable output of `raft message check`."""

from __future__ import annotations

import re

from base_agent.integrations.raft.errors import RaftProtocolError
from base_agent.integrations.raft.models import RaftMessage

_HEADER = re.compile(
    r"(?m)^\[target=(?P<target>\S+) "
    r"msg=(?P<message_id>\S+) "
    r"time=(?P<timestamp>.*?) "
    r"type=(?P<sender_type>[^\]\s]+)\] "
)
_SENDER = re.compile(
    r"^@(?P<handle>[^\s:]+)(?: — [^:\n]*)?: (?P<content>.*)$",
    re.DOTALL,
)
_TASK = re.compile(
    r"\s+\[task #(?P<number>\d+) "
    r"status=(?P<status>[a-z_]+)"
    r"(?: assignee=(?P<assignee_type>[a-z_]+):"
    r"(?P<assignee_id>[^\]\s]+))?\]\s*$",
    re.DOTALL,
)
_DRAIN_SUFFIXES = (
    "\nNo more new messages.",
    "\nMore messages are pending. Run `raft message check` again.",
)


def parse_raft_messages(output: str) -> tuple[RaftMessage, ...]:
    """Parse the stable text envelope emitted by the Raft Agent CLI."""
    normalized = output.rstrip()
    if normalized in {"", "No new messages."}:
        return ()
    for suffix in _DRAIN_SUFFIXES:
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip()
            break
    if not normalized or normalized == "No new messages.":
        return ()

    matches = tuple(_HEADER.finditer(normalized))
    if not matches:
        raise RaftProtocolError(
            "raft message check returned output without a recognized message envelope"
        )

    messages: list[RaftMessage] = []
    for index, match in enumerate(matches):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        body = normalized[match.end() : body_end].rstrip()
        sender = _SENDER.fullmatch(body)
        if sender is None:
            raise RaftProtocolError(
                "raft message check returned a message with an unsupported sender envelope"
            )
        content = sender.group("content").rstrip()
        task = _TASK.search(content)
        task_number: int | None = None
        task_status: str | None = None
        task_assignee_type: str | None = None
        task_assignee_id: str | None = None
        if task is not None:
            task_number = int(task.group("number"))
            task_status = task.group("status")
            task_assignee_type = task.group("assignee_type")
            task_assignee_id = task.group("assignee_id")
            content = content[: task.start()].rstrip()
        message_id = match.group("message_id")
        if message_id == "-":
            raise RaftProtocolError("raft message envelope is missing its message id")
        messages.append(
            RaftMessage(
                target=match.group("target"),
                message_id=message_id,
                timestamp=match.group("timestamp"),
                sender_type=match.group("sender_type"),
                sender_handle=sender.group("handle"),
                content=content,
                task_number=task_number,
                task_status=task_status,
                task_assignee_type=task_assignee_type,
                task_assignee_id=task_assignee_id,
            )
        )
    return tuple(messages)
