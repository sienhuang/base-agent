"""Provider contract for ranked Web Search results."""

from typing import Protocol, runtime_checkable

from base_agent.web_search.models import WebSearchQuery, WebSearchResponse


@runtime_checkable
class WebSearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def search(self, request: WebSearchQuery) -> WebSearchResponse: ...
