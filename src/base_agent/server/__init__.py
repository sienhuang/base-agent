"""Optional FastAPI adapter; install base-agent[server] before importing."""

from base_agent.server.app import create_app
from base_agent.server.schemas import ResumeRunRequest, StartRunRequest, StartRunResponse
from base_agent.server.tasks import RunTaskManager

__all__ = [
    "ResumeRunRequest",
    "RunTaskManager",
    "StartRunRequest",
    "StartRunResponse",
    "create_app",
]
