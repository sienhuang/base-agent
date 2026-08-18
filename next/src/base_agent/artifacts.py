"""Host-owned references to artifacts produced or consumed by a Flow."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A stable logical reference; the Host owns and resolves its location."""

    name: str
    location: str
    media_type: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("artifact name must not be blank")
        if not self.location.strip():
            raise ValueError("artifact location must not be blank")
        if not self.media_type.strip():
            raise ValueError("artifact media_type must not be blank")
