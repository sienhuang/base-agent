"""Provider protocol consumed by the agent runtime."""

from typing import Protocol, runtime_checkable

from base_agent.models import ModelRequest, ModelResponse


@runtime_checkable
class ModelProvider(Protocol):
    """Asynchronous model boundary implemented by provider adapters."""

    @property
    def name(self) -> str: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
