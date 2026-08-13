"""Optional FastAPI adapter for process-local background Runs."""

from base_agent.server.app import create_http_app
from base_agent.server.registry import (
    FlowNotRegisteredError,
    FlowRunNotFoundError,
    InMemoryFlowRegistry,
    InMemoryRunRegistry,
    RunNotFoundError,
)
from base_agent.server.schemas import (
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
    "InMemoryRunRegistry",
    "InMemoryFlowRegistry",
    "EventResponse",
    "FlowChildRunResponse",
    "FlowNotRegisteredError",
    "FlowResponse",
    "FlowRunNotFoundError",
    "RunNotFoundError",
    "RunResponse",
    "StartRunRequest",
    "StartRunResponse",
    "StartFlowRequest",
    "StartFlowResponse",
    "create_http_app",
]
