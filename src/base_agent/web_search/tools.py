"""Model-facing Web Search Tool and its concrete composition bundle."""

from __future__ import annotations

from dataclasses import dataclass

from base_agent.tools import FunctionTool, tool
from base_agent.web_search.models import WebSearchFreshness, WebSearchQuery
from base_agent.web_search.protocol import WebSearchProvider


@dataclass(frozen=True, slots=True)
class WebSearchBundle:
    """One configured Web Search Tool with explicit permission requirements."""

    tools: tuple[FunctionTool, ...]
    tool_names: tuple[str, ...]
    required_permissions: frozenset[str]


def web_search_tools(
    provider: WebSearchProvider,
    *,
    max_results: int = 10,
    max_snippet_characters: int = 1_000,
    timeout_seconds: float = 20.0,
) -> tuple[FunctionTool, ...]:
    """Build a bounded Web Search Tool over one application-owned provider."""
    if max_results < 1 or max_results > 20:
        raise ValueError("max_results must be between 1 and 20")
    if max_snippet_characters < 1 or max_snippet_characters > 4_000:
        raise ValueError("max_snippet_characters must be between 1 and 4000")

    @tool(
        name="web_search",
        permissions=frozenset({"web:search"}),
        timeout_seconds=timeout_seconds,
    )
    async def search(
        query: str,
        limit: int = 5,
        domains: list[str] | None = None,
        freshness: WebSearchFreshness | None = None,
        country: str | None = None,
        search_language: str | None = None,
    ) -> dict[str, object]:
        """Search the public web and return bounded titles, URLs, snippets, and sources."""
        if limit > max_results:
            raise ValueError(f"limit must not exceed {max_results}")
        request = WebSearchQuery(
            query=query,
            limit=limit,
            domains=tuple(domains or ()),
            freshness=freshness,
            country=country,
            search_language=search_language,
        )
        response = await provider.search(request)
        selected = response.results[: request.limit]
        results: list[dict[str, object]] = []
        for item in selected:
            snippet = item.snippet[:max_snippet_characters]
            results.append(
                {
                    **item.model_dump(mode="json", exclude={"snippet"}),
                    "snippet": snippet,
                    "snippet_truncated": len(item.snippet) > len(snippet),
                }
            )
        return {
            "provider": response.provider,
            "query": request.query,
            "results": results,
            "result_count": len(results),
            "has_more": response.has_more or len(response.results) > len(selected),
        }

    return (search,)


def web_search_bundle(
    provider: WebSearchProvider,
    *,
    max_results: int = 10,
    max_snippet_characters: int = 1_000,
    timeout_seconds: float = 20.0,
) -> WebSearchBundle:
    """Compose the concrete Web Search Tool without granting its permission."""
    tools = web_search_tools(
        provider,
        max_results=max_results,
        max_snippet_characters=max_snippet_characters,
        timeout_seconds=timeout_seconds,
    )
    return WebSearchBundle(
        tools=tools,
        tool_names=tuple(candidate.definition.name for candidate in tools),
        required_permissions=frozenset().union(
            *(candidate.permissions for candidate in tools)
        ),
    )
