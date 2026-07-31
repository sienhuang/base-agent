"""Provider-neutral Web Search contracts, Tools, and first-party adapters."""

from base_agent.web_search.brave import BraveSearchTransport, BraveWebSearchProvider
from base_agent.web_search.errors import (
    InvalidWebSearchResponseError,
    WebSearchProviderError,
    WebSearchResponseLimitError,
    WebSearchTransportError,
)
from base_agent.web_search.models import (
    WebSearchFreshness,
    WebSearchQuery,
    WebSearchResponse,
    WebSearchResult,
)
from base_agent.web_search.protocol import WebSearchProvider
from base_agent.web_search.tools import (
    WebSearchBundle,
    web_search_bundle,
    web_search_tools,
)

__all__ = [
    "BraveSearchTransport",
    "BraveWebSearchProvider",
    "InvalidWebSearchResponseError",
    "WebSearchBundle",
    "WebSearchFreshness",
    "WebSearchProvider",
    "WebSearchProviderError",
    "WebSearchQuery",
    "WebSearchResponse",
    "WebSearchResponseLimitError",
    "WebSearchResult",
    "WebSearchTransportError",
    "web_search_bundle",
    "web_search_tools",
]
