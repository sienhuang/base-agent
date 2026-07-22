"""Typed resource lifecycle records."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class ResourcePhase(StrEnum):
    ACQUIRE = "acquire"
    RELEASE = "release"


class ResourceFailure(BaseModel):
    """A visible acquisition or cleanup failure for one named resource."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    phase: ResourcePhase
    message: str
