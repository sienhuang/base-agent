"""Network-free Provider test doubles for Agent and Flow tests."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from base_agent.governance import AllowAllConfirmations
from base_agent.models import ModelRequest, ModelResponse
from base_agent.providers import ProviderCapabilities, ProviderInfo


class FakeModelExhaustedError(RuntimeError):
    pass


class FakeModel:
    def __init__(
        self,
        responses: Iterable[ModelResponse],
        *,
        name: str = "fake-model",
        model: str | None = None,
        capabilities: ProviderCapabilities | None = None,
    ) -> None:
        self._info = ProviderInfo(
            name=name,
            default_model=model or name,
            capabilities=capabilities
            or ProviderCapabilities(
                tools=True,
                structured_output=True,
            ),
        )
        self._responses = deque(responses)
        self._requests: list[ModelRequest] = []

    @classmethod
    def text(cls, content: str, *, name: str = "fake-model") -> FakeModel:
        return cls([ModelResponse(content=content)], name=name)

    @property
    def info(self) -> ProviderInfo:
        return self._info

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    @property
    def remaining_responses(self) -> int:
        return len(self._responses)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._requests.append(request)
        if not self._responses:
            raise FakeModelExhaustedError("FakeModel has no scripted responses remaining")
        return self._responses.popleft()


__all__ = [
    "AllowAllConfirmations",
    "FakeModel",
    "FakeModelExhaustedError",
]
