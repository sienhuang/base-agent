"""The narrow provider boundary used by the Harness."""

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from base_agent.models import ModelRequest, ModelResponse, ModelStreamEvent
from base_agent.providers.models import ProviderInfo


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def info(self) -> ProviderInfo: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class StreamingModelProvider(ModelProvider, Protocol):
    def stream(
        self,
        request: ModelRequest,
    ) -> AsyncIterator[ModelStreamEvent]: ...
