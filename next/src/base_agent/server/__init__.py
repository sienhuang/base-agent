"""FastAPI adapters for local or independently queued execution."""

from base_agent.server.app import OperatorAuthorizer, create_http_app
from base_agent.server.application import AgentApplication, Preflight, Shutdown
from base_agent.server.executor import LocalRunExecutor
from base_agent.server.queued_executor import QueuedRunExecutor
from base_agent.server.registry import (
    FlowNotRegisteredError,
    FlowRunNotFoundError,
    InMemoryFlowRegistry,
    InMemoryRunRegistry,
    RegisteredFlow,
    RunNotFoundError,
)
from base_agent.server.schemas import (
    ArtifactRefResponse,
    ArtifactResponse,
    EventResponse,
    FlowChildRunResponse,
    FlowResponse,
    RunResponse,
    StartFlowRequest,
    StartFlowResponse,
    StartRunRequest,
    StartRunResponse,
)
from base_agent.server.worker import ExecutionWorker, FencedEventStore

__all__ = [
    "AgentApplication",
    "ArtifactRefResponse",
    "ArtifactResponse",
    "EventResponse",
    "ExecutionWorker",
    "FencedEventStore",
    "FlowChildRunResponse",
    "FlowNotRegisteredError",
    "FlowResponse",
    "FlowRunNotFoundError",
    "InMemoryFlowRegistry",
    "InMemoryRunRegistry",
    "LocalRunExecutor",
    "OperatorAuthorizer",
    "Preflight",
    "QueuedRunExecutor",
    "RegisteredFlow",
    "RunNotFoundError",
    "RunResponse",
    "Shutdown",
    "StartRunRequest",
    "StartRunResponse",
    "StartFlowRequest",
    "StartFlowResponse",
    "create_http_app",
]
