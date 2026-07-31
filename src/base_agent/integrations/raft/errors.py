"""Typed failures raised by the optional Raft CLI integration."""

from __future__ import annotations


class RaftIntegrationError(RuntimeError):
    """Base error for Raft transport and protocol failures."""


class RaftCliNotFoundError(RaftIntegrationError):
    """The configured Raft CLI executable could not be started."""


class RaftCliCommandError(RaftIntegrationError):
    """A Raft CLI command returned a non-zero exit status."""

    def __init__(
        self,
        command: str,
        returncode: int,
        stderr: str,
    ) -> None:
        detail = stderr.strip()[:2_000] or "no error details"
        super().__init__(
            f"raft CLI command '{command}' failed with exit code "
            f"{returncode}: {detail}"
        )
        self.command = command
        self.returncode = returncode
        self.stderr = stderr


class RaftCliTimeoutError(RaftIntegrationError):
    """A Raft CLI command exceeded its configured timeout."""


class RaftCliOutputLimitError(RaftIntegrationError):
    """A Raft CLI command exceeded its configured output limit."""


class RaftProtocolError(RaftIntegrationError):
    """Raft CLI output or wake input did not match the supported contract."""


class RaftBridgeExitedError(RaftIntegrationError):
    """The long-lived Raft wake bridge stopped unexpectedly."""


class RaftStateError(RaftIntegrationError):
    """The Worker's local recovery state could not be read or written."""
