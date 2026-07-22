import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from base_agent import Agent, AgentProfile, AgentResultStatus, ModelResponse, ResourceSpec, ToolCall
from base_agent.browser import BrowserActionResult, BrowserSession, BrowserSnapshot, browser_tools
from base_agent.browser.playwright import (
    BrowserNetworkPolicy,
    BrowserPolicyError,
    PlaywrightBrowserConfig,
    PlaywrightBrowserSession,
)
from base_agent.testing import FakeModel

BROWSER_CHANNEL = os.getenv("BASE_AGENT_TEST_BROWSER_CHANNEL")
requires_browser = pytest.mark.skipif(
    BROWSER_CHANNEL is None,
    reason="set BASE_AGENT_TEST_BROWSER_CHANNEL to run Playwright integration tests",
)


class FakeBrowser:
    async def navigate(self, url: str) -> BrowserActionResult:
        return BrowserActionResult(url=url, title="Fake", detail="navigated")

    async def snapshot(self) -> BrowserSnapshot:
        return BrowserSnapshot(url="https://example.test", title="Fake", text="content")

    async def click(self, selector: str) -> BrowserActionResult:
        return BrowserActionResult(url="https://example.test", title="Fake", detail=selector)

    async def fill(self, selector: str, text: str) -> BrowserActionResult:
        return BrowserActionResult(
            url="https://example.test", title="Fake", detail=f"{selector}:{text}"
        )

    async def press(self, key: str) -> BrowserActionResult:
        return BrowserActionResult(url="https://example.test", title="Fake", detail=key)

    async def select_option(self, selector: str, value: str) -> BrowserActionResult:
        return BrowserActionResult(
            url="https://example.test", title="Fake", detail=f"{selector}:{value}"
        )

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        del full_page
        return b"\x89PNG\r\n\x1a\nfake"


@pytest.mark.asyncio
async def test_browser_policy_blocks_unsafe_schemes_and_private_networks() -> None:
    policy = BrowserNetworkPolicy()

    with pytest.raises(BrowserPolicyError, match="must use HTTP"):
        await policy.check("file:///etc/passwd")
    with pytest.raises(BrowserPolicyError, match="non-public"):
        await policy.check("http://127.0.0.1/private")
    with pytest.raises(BrowserPolicyError, match="allowed_hosts"):
        await BrowserNetworkPolicy(allowed_hosts=("example.com",)).check(
            "https://openai.com"
        )


@pytest.mark.asyncio
async def test_browser_screenshot_tool_creates_run_owned_artifact() -> None:
    browser = FakeBrowser()

    @asynccontextmanager
    async def resource(context: Any) -> AsyncIterator[FakeBrowser]:
        del context
        yield browser

    model = FakeModel(
        [
            ModelResponse(
                tool_calls=(
                    ToolCall(
                        id="capture",
                        name="browser_screenshot",
                        arguments={"name": "page.png", "full_page": True},
                    ),
                )
            ),
            ModelResponse(content="captured"),
        ]
    )
    agent = Agent(
        profile=AgentProfile(
            id="browser-agent",
            instructions="Capture the page.",
            tools=("browser_screenshot",),
            permissions=frozenset({"browser:capture"}),
        ),
        model=model,
        tools=browser_tools(),
        resources=(ResourceSpec("browser", resource),),
    )

    result = await agent.run("capture")

    assert isinstance(browser, BrowserSession)
    assert result.status is AgentResultStatus.COMPLETED
    assert len(result.artifacts) == 1
    assert result.artifacts[0].name == "page.png"
    assert await agent.read_content(result.artifacts[0].id) == b"\x89PNG\r\n\x1a\nfake"


@requires_browser
@pytest.mark.asyncio
async def test_real_playwright_browser_navigates_interacts_and_captures() -> None:
    assert BROWSER_CHANNEL is not None
    async with local_page() as url:
        session = await PlaywrightBrowserSession.create(
            PlaywrightBrowserConfig(
                channel=BROWSER_CHANNEL,
                network_policy=BrowserNetworkPolicy(
                    allowed_hosts=("127.0.0.1",),
                    allow_private_network=True,
                ),
            )
        )
        try:
            navigated = await session.navigate(url)
            await session.fill("#name", "Ada")
            await session.click("#greet")
            snapshot = await session.snapshot()
            screenshot = await session.screenshot(full_page=True)
        finally:
            await session.close()

    assert navigated.title == "Browser Test"
    assert "Hello Ada" in snapshot.text
    assert screenshot.startswith(b"\x89PNG\r\n\x1a\n")


@asynccontextmanager
async def local_page() -> AsyncIterator[str]:
    body = (
        b"<!doctype html><html><head><title>Browser Test</title></head><body>"
        b'<label>Name <input id="name"></label><button id="greet" '
        b'onclick="document.querySelector(\'#output\').textContent = \'Hello \' + '
        b'document.querySelector(\'#name\').value">Greet</button>'
        b'<div id="output">Waiting</div></body></html>'
    )

    async def handler(reader: Any, writer: Any) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/html; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode()
                + b"Connection: close\r\n\r\n"
                + body
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield f"http://127.0.0.1:{port}/"
