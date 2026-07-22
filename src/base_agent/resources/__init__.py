"""Application-defined resources scoped to one execution segment."""

from base_agent.resources.manager import (
    ResourceAcquisitionError,
    ResourceError,
    ResourceFactory,
    ResourceManager,
    ResourceNotFoundError,
    ResourceScopeClosedError,
    ResourceSpec,
    ResourceTaskMismatchError,
    ResourceTypeError,
)
from base_agent.resources.models import ResourceFailure, ResourcePhase

__all__ = [
    "ResourceAcquisitionError",
    "ResourceError",
    "ResourceFactory",
    "ResourceFailure",
    "ResourceManager",
    "ResourceNotFoundError",
    "ResourcePhase",
    "ResourceScopeClosedError",
    "ResourceSpec",
    "ResourceTaskMismatchError",
    "ResourceTypeError",
]
