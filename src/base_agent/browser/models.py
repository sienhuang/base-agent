"""Provider-neutral Browser values."""

from pydantic import BaseModel, ConfigDict, Field


class BrowserSnapshot(BaseModel):
    """Bounded textual state of the active page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    title: str
    text: str
    truncated: bool = False


class BrowserActionResult(BaseModel):
    """Page identity after one interaction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    title: str
    detail: str = Field(min_length=1)
