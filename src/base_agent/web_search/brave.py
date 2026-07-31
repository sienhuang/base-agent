"""Brave Web Search API adapter implemented with the Python standard library."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import SecretStr, ValidationError

from base_agent.web_search.errors import (
    InvalidWebSearchResponseError,
    WebSearchResponseLimitError,
    WebSearchTransportError,
)
from base_agent.web_search.models import (
    WebSearchFreshness,
    WebSearchQuery,
    WebSearchResponse,
    WebSearchResult,
)

_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
_FRESHNESS = {
    WebSearchFreshness.DAY: "pd",
    WebSearchFreshness.WEEK: "pw",
    WebSearchFreshness.MONTH: "pm",
    WebSearchFreshness.YEAR: "py",
}


class BraveSearchTransport(Protocol):
    """Injectable HTTP boundary used by deterministic adapter tests."""

    def __call__(
        self,
        url: str,
        params: Mapping[str, str],
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> Awaitable[dict[str, Any]]: ...


class BraveWebSearchProvider:
    """Normalize Brave Web Search API results into the core provider contract."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
        transport: BraveSearchTransport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Brave Search API key must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be greater than zero")
        self._api_key = SecretStr(api_key.strip())
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._transport = transport or _get_json

    @property
    def name(self) -> str:
        return "brave-web-search"

    async def search(self, request: WebSearchQuery) -> WebSearchResponse:
        query = _query_with_domains(request)
        params = {
            "q": query,
            "count": str(request.limit),
            "result_filter": "web",
            "safesearch": "moderate",
            "text_decorations": "false",
        }
        if request.freshness is not None:
            params["freshness"] = _FRESHNESS[request.freshness]
        if request.country is not None:
            params["country"] = request.country
        if request.search_language is not None:
            params["search_lang"] = request.search_language
        payload = await self._transport(
            _ENDPOINT,
            params,
            {
                "Accept": "application/json",
                "X-Subscription-Token": self._api_key.get_secret_value(),
            },
            self._timeout_seconds,
            self._max_response_bytes,
        )
        return _parse_response(payload, limit=request.limit, provider=self.name)


def _query_with_domains(request: WebSearchQuery) -> str:
    if not request.domains:
        return request.query
    filters = " OR ".join(f"site:{domain}" for domain in request.domains)
    query = f"{request.query} ({filters})"
    if len(query) > 400:
        raise ValueError("search query with domain filters exceeds 400 characters")
    return query


def _parse_response(
    payload: Mapping[str, Any],
    *,
    limit: int,
    provider: str,
) -> WebSearchResponse:
    raw_web = payload.get("web")
    if raw_web is None:
        raw_results: object = ()
    elif isinstance(raw_web, dict):
        raw_results = raw_web.get("results", ())
    else:
        raise InvalidWebSearchResponseError("Brave response field 'web' must be an object")
    if not isinstance(raw_results, (list, tuple)):
        raise InvalidWebSearchResponseError(
            "Brave response field 'web.results' must be an array"
        )

    results: list[WebSearchResult] = []
    for raw in raw_results[:limit]:
        if not isinstance(raw, dict):
            raise InvalidWebSearchResponseError("Brave search result must be an object")
        title = raw.get("title")
        url = raw.get("url")
        description = raw.get("description", "")
        if not isinstance(title, str) or not isinstance(url, str):
            raise InvalidWebSearchResponseError(
                "Brave search result requires string title and URL"
            )
        if not isinstance(description, str):
            raise InvalidWebSearchResponseError(
                "Brave search result description must be a string"
            )
        profile = raw.get("profile")
        profile_name = profile.get("long_name") if isinstance(profile, dict) else None
        source = (
            profile_name
            if isinstance(profile_name, str)
            else urlsplit(url).hostname
        )
        published_value = raw.get("page_age", raw.get("age"))
        try:
            result = WebSearchResult(
                title=title,
                url=url,
                snippet=description,
                source=source,
                published=published_value if isinstance(published_value, str) else None,
            )
        except ValidationError as exc:
            raise InvalidWebSearchResponseError(
                "Brave search result violated the normalized result contract"
            ) from exc
        results.append(result)

    query_data = payload.get("query")
    has_more = (
        bool(query_data.get("more_results_available"))
        if isinstance(query_data, dict)
        else False
    )
    return WebSearchResponse(
        provider=provider,
        results=tuple(results),
        has_more=has_more or len(raw_results) > limit,
    )


async def _get_json(
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _get_json_sync,
        url,
        params,
        headers,
        timeout_seconds,
        max_response_bytes,
    )


def _get_json_sync(
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> dict[str, Any]:
    request = Request(f"{url}?{urlencode(params)}", headers=dict(headers))
    opener = build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            content = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        raise WebSearchTransportError(
            f"Brave Search request failed with HTTP status {exc.code}"
        ) from exc
    except (OSError, URLError) as exc:
        raise WebSearchTransportError("Brave Search request failed") from exc
    if len(content) > max_response_bytes:
        raise WebSearchResponseLimitError(
            f"Brave Search response exceeded {max_response_bytes} bytes"
        )
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidWebSearchResponseError(
            "Brave Search response was not valid UTF-8 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidWebSearchResponseError("Brave Search response must be an object")
    return payload


class _RejectRedirects(HTTPRedirectHandler):
    """Keep the subscription token on the fixed Brave API origin."""

    def redirect_request(
        self,
        request: Request,
        file_pointer: Any,
        code: int,
        message: str,
        headers: Any,
        new_url: str,
    ) -> None:
        del request, file_pointer, code, message, headers, new_url
        return None
