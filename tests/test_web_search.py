from collections.abc import Mapping
from typing import Any

import pytest

from base_agent import ToolCall, ToolExecutor, ToolRegistry
from base_agent.web_search import (
    BraveWebSearchProvider,
    WebSearchFreshness,
    WebSearchProvider,
    WebSearchQuery,
    WebSearchResponse,
    WebSearchResult,
    web_search_bundle,
)


class FakeWebSearchProvider:
    name = "fake-web-search"

    def __init__(self) -> None:
        self.requests: list[WebSearchQuery] = []

    async def search(self, request: WebSearchQuery) -> WebSearchResponse:
        self.requests.append(request)
        return WebSearchResponse(
            provider=self.name,
            results=(
                WebSearchResult(
                    title="First result",
                    url="https://example.com/first",
                    snippet="abcdefghijk",
                    source="example.com",
                ),
                WebSearchResult(
                    title="Second result",
                    url="https://example.org/second",
                    snippet="second",
                    source="example.org",
                ),
            ),
            has_more=True,
        )


@pytest.mark.asyncio
async def test_web_search_bundle_enforces_permission_and_bounds_results() -> None:
    provider = FakeWebSearchProvider()
    bundle = web_search_bundle(
        provider,
        max_results=5,
        max_snippet_characters=8,
    )
    executor = ToolExecutor(ToolRegistry(bundle.tools))
    call = ToolCall(
        id="search-1",
        name="web_search",
        arguments={
            "query": "data lineage",
            "limit": 2,
            "domains": ["example.com"],
            "freshness": "week",
        },
    )

    denied = await executor.execute(call)
    successful = await executor.execute(
        call,
        granted_permissions=bundle.required_permissions,
    )

    assert isinstance(provider, WebSearchProvider)
    assert denied.error_code == "permission_denied"
    assert successful.data["result_count"] == 2
    assert successful.data["has_more"] is True
    assert successful.data["results"][0]["snippet"] == "abcdefgh"
    assert successful.data["results"][0]["snippet_truncated"] is True
    assert provider.requests == [
        WebSearchQuery(
            query="data lineage",
            limit=2,
            domains=("example.com",),
            freshness=WebSearchFreshness.WEEK,
        )
    ]
    assert bundle.tool_names == ("web_search",)
    assert bundle.required_permissions == frozenset({"web:search"})


class RecordingTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "url": url,
                "params": dict(params),
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        return {
            "query": {"more_results_available": False},
            "web": {
                "results": [
                    {
                        "title": "Official documentation",
                        "url": "https://docs.example.com/data",
                        "description": "Reference documentation.",
                        "profile": {"long_name": "Example Docs"},
                        "page_age": "2026-01-01T00:00:00Z",
                    }
                ]
            },
        }


@pytest.mark.asyncio
async def test_brave_provider_maps_request_and_response_without_exposing_key() -> None:
    transport = RecordingTransport()
    provider = BraveWebSearchProvider(
        "secret-search-key",
        timeout_seconds=7,
        max_response_bytes=123_456,
        transport=transport,
    )

    response = await provider.search(
        WebSearchQuery(
            query="data contracts",
            limit=3,
            domains=("docs.example.com",),
            freshness=WebSearchFreshness.MONTH,
            country="US",
            search_language="en",
        )
    )
    call = transport.calls[0]

    assert response.provider == "brave-web-search"
    assert response.results[0].source == "Example Docs"
    assert response.results[0].published == "2026-01-01T00:00:00Z"
    assert call["params"] == {
        "q": "data contracts (site:docs.example.com)",
        "count": "3",
        "result_filter": "web",
        "safesearch": "moderate",
        "text_decorations": "false",
        "freshness": "pm",
        "country": "US",
        "search_lang": "en",
    }
    assert call["headers"] == {
        "Accept": "application/json",
        "X-Subscription-Token": "secret-search-key",
    }
    assert call["timeout_seconds"] == 7
    assert call["max_response_bytes"] == 123_456
    assert "secret-search-key" not in repr(provider)


def test_web_search_query_rejects_unsafe_domains() -> None:
    with pytest.raises(ValueError, match="invalid search domain"):
        WebSearchQuery(query="test", domains=("https://example.com/path",))
