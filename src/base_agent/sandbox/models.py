"""Provider-neutral Sandbox values."""

from pydantic import BaseModel, ConfigDict, Field


class SandboxCommandResult(BaseModel):
    """Bounded output from one isolated argv execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    duration_ms: int = Field(ge=0)


class SandboxFileContent(BaseModel):
    """Text read from a path inside one Sandbox workspace."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    content: str
    truncated: bool = False
