"""Optional FastAPI adapter for process-local background Runs."""

from base_agent.server.app import create_http_app
from base_agent.server.registry import (
    FlowNotRegisteredError,
    FlowRunNotFoundError,
    InMemoryFlowRegistry,
    InMemoryRunRegistry,
    RegisteredFlow,
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
    "RegisteredFlow",
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
