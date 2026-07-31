"""Optional Raft External Agent control-plane adapter."""

from base_agent.integrations.raft.client import (
    RaftBridge,
    RaftCliClient,
    RaftClient,
)
from base_agent.integrations.raft.errors import (
    RaftBridgeExitedError,
    RaftCliCommandError,
    RaftCliNotFoundError,
    RaftCliOutputLimitError,
    RaftCliTimeoutError,
    RaftIntegrationError,
    RaftProtocolError,
    RaftStateError,
)
from base_agent.integrations.raft.models import (
    RaftDrainResult,
    RaftInboxBatch,
    RaftMessage,
    RaftWorkerConfig,
)
from base_agent.integrations.raft.parser import parse_raft_messages
from base_agent.integrations.raft.wake import RaftWakeServer
from base_agent.integrations.raft.worker import RaftWorker

__all__ = [
    "RaftBridge",
    "RaftBridgeExitedError",
    "RaftCliClient",
    "RaftCliCommandError",
    "RaftCliNotFoundError",
    "RaftCliOutputLimitError",
    "RaftCliTimeoutError",
    "RaftClient",
    "RaftDrainResult",
    "RaftInboxBatch",
    "RaftIntegrationError",
    "RaftMessage",
    "RaftProtocolError",
    "RaftStateError",
    "RaftWakeServer",
    "RaftWorker",
    "RaftWorkerConfig",
    "parse_raft_messages",
]
