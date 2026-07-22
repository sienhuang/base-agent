# Browser Sessions and Playwright Adapter

The provider-neutral `BrowserSession` supports navigation, bounded text snapshots, selector-based
interaction, and PNG capture. `browser_tools()` uses existing Resource, permission, timeout, event,
and Artifact paths; it does not introduce a Browser-specific Agent subclass.

## Install

```bash
uv add 'base-agent[browser]'
uv run playwright install chromium
```

Playwright browser binaries are intentionally not bundled in the base-agent wheel. A deployment may
instead select an installed branded browser with `channel="chrome"` or `channel="msedge"`.

## Configure an Agent

```python
from base_agent.browser import browser_tools
from base_agent.browser.playwright import (
    BrowserNetworkPolicy,
    PlaywrightBrowserConfig,
    playwright_browser_resource,
)

config = PlaywrightBrowserConfig(
    network_policy=BrowserNetworkPolicy(
        allowed_hosts=("docs.example.com", "*.static.example.com"),
    )
)

agent = Agent(
    profile=AgentProfile(
        id="browser-agent",
        instructions="Use the browser only for approved documentation sites.",
        tools=(
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_fill",
            "browser_press",
            "browser_select_option",
            "browser_screenshot",
        ),
        permissions=frozenset(
            {"browser:navigate", "browser:read", "browser:interact", "browser:capture"}
        ),
    ),
    model=model,
    tools=browser_tools(),
    resources=(playwright_browser_resource(config),),
)
```

Screenshots are written through `ArtifactManager` and returned as Run-owned Artifact references;
base64 image data is not injected into Tool messages.

## Network and action policy

- direct navigation accepts only HTTP, HTTPS, WS, and WSS URLs;
- every BrowserContext request is intercepted and checked, including popup traffic;
- private, loopback, link-local, multicast, reserved, and otherwise non-global addresses are denied
  by default;
- `allowed_hosts` supports exact hosts and `*.example.com` subdomain patterns;
- private-network access requires an explicit `allow_private_network=True` setting;
- the active page URL is checked again before snapshots, screenshots, and interaction results;
- arbitrary JavaScript execution, browser security disabling, downloads, uploads, extensions, CDP,
  VNC, and host-profile reuse are not exposed by the default Tool set.

DNS resolution checks are not a complete defense against DNS rebinding or a malicious proxy. Use a
network egress proxy/firewall and application authorization for high-risk deployments.

## Lifecycle

Each Resource acquisition creates a new isolated BrowserContext and page. It closes on completion,
failure, cancellation, or `WAITING`. Cookies and page state therefore do not survive resume unless
the host supplies an externally managed `BrowserSession` implementation. Live Playwright objects are
never serialized into checkpoints.

## Integration test

Use an installed Playwright browser channel:

```bash
BASE_AGENT_TEST_BROWSER_CHANNEL='chrome' uv run pytest tests/test_browser.py
```
