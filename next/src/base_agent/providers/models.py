"""Provider identity and capability declarations."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """Features an Adapter can faithfully map to its upstream API."""

    tools: bool = False
    structured_output: bool = False
    streaming: bool = False


@dataclass(frozen=True, slots=True)
class ProviderInfo:
    """Stable Provider identity used for model resolution and audit snapshots."""

    name: str
    default_model: str | None = None
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("provider name must not be blank")
        if self.default_model is not None and not self.default_model.strip():
            raise ValueError("provider default_model must not be blank")
