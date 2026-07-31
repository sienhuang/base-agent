"""Stable failures raised by Web Search providers."""


class WebSearchProviderError(RuntimeError):
    """Base failure raised by a WebSearchProvider implementation."""


class WebSearchTransportError(WebSearchProviderError):
    """A search service could not be reached or returned an HTTP failure."""


class WebSearchResponseLimitError(WebSearchProviderError):
    """A search service returned more bytes than the configured limit."""


class InvalidWebSearchResponseError(WebSearchProviderError):
    """A search service returned an unsupported response shape."""
