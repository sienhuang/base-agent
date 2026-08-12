"""Optional FastAPI adapter for process-local background Runs."""

from base_agent.server.app import create_http_app
from base_agent.server.registry import InMemoryRunRegistry, RunNotFoundError
from base_agent.server.schemas import EventResponse, RunResponse, StartRunRequest, StartRunResponse

__all__ = [
    "InMemoryRunRegistry",
    "EventResponse",
    "RunNotFoundError",
    "RunResponse",
    "StartRunRequest",
    "StartRunResponse",
    "create_http_app",
]
