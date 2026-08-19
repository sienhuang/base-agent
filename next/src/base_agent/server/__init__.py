"""FastAPI adapter for local execution and persistent Event read recovery."""

from base_agent.server.app import create_http_app
from base_agent.server.application import AgentApplication, Preflight, Shutdown
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

__all__ = [
    "AgentApplication",
    "ArtifactRefResponse",
    "ArtifactResponse",
    "InMemoryRunRegistry",
    "Preflight",
    "RegisteredFlow",
    "InMemoryFlowRegistry",
    "EventResponse",
    "FlowChildRunResponse",
    "FlowNotRegisteredError",
    "FlowResponse",
    "FlowRunNotFoundError",
    "RunNotFoundError",
    "RunResponse",
    "Shutdown",
    "StartRunRequest",
    "StartRunResponse",
    "StartFlowRequest",
    "StartFlowResponse",
    "create_http_app",
]
