"""A deterministic, network-free model provider for tests and examples."""

from collections import deque
from collections.abc import Iterable

from base_agent.models import ModelRequest, ModelResponse


class FakeModelExhaustedError(RuntimeError):
    """Raised when code calls a FakeModel after all scripted responses are consumed."""


class FakeModel:
    """Return scripted responses in order and retain received requests for assertions."""

    def __init__(
        self,
        responses: Iterable[ModelResponse],
        *,
        name: str = "fake-model",
    ) -> None:
        self._name = name
        self._responses = deque(responses)
        self._requests: list[ModelRequest] = []

    @property
    def name(self) -> str:
        return self._name

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
