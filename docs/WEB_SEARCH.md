# Web Search

Web Search is a first-party, opt-in Tool rather than a model-provider side effect. The model backend
returns a `web_search` ToolCall, and the normal ToolExecutor applies allowlists, the `web:search`
permission, timeout, Events, and supervision.

## Provider-neutral composition

Implement `WebSearchProvider` for an internal or external search service:

```python
from base_agent import Agent, AgentProfile, web_search_bundle

search = web_search_bundle(
    provider,
    max_results=10,
    max_snippet_characters=1_000,
)

agent = Agent(
    profile=AgentProfile(
        id="research-agent",
        instructions="Cite the URLs returned by Web Search.",
        tools=search.tool_names,
        permissions=frozenset({"web:search"}),
    ),
    model=model,
    tools=search.tools,
)
```

Requests support a bounded result count, up to five domain filters, a portable freshness window,
country, and search language. Results contain title, HTTP(S) URL, bounded snippet, source, and
optional publication information.

## Brave adapter

`BraveWebSearchProvider` is the first concrete adapter:

```python
import os

from base_agent import BraveWebSearchProvider, web_search_bundle

provider = BraveWebSearchProvider(os.environ["BRAVE_SEARCH_API_KEY"])
search = web_search_bundle(provider)
```

The API key remains inside the Provider and is never a model-facing Tool argument. The adapter uses
the documented HTTPS endpoint, `X-Subscription-Token`, a maximum of 20 results, bounded response
bytes, and typed transport/response failures. It has no third-party Python dependency.

Web Search discovers sources; it does not fetch arbitrary result URLs. Known-URL fetching requires
a separate Tool with SSRF, DNS, redirect, content-type, and body-size policy. Interactive or
JavaScript-rendered pages belong to `browser_tools()`.

Official API reference:
[Brave Web Search API](https://api-dashboard.search.brave.com/api-reference/web/search/get).
