"""Execution-scoped Playwright Browser implementation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from base_agent.browser.models import BrowserActionResult, BrowserSnapshot
from base_agent.resources import ResourceSpec
from base_agent.runtime.context import RuntimeContext


class BrowserPolicyError(PermissionError):
    """A URL was rejected by the configured network policy."""


class BrowserNetworkPolicy(BaseModel):
    """Network destinations permitted for one Browser context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_hosts: tuple[str, ...] = ()
    allow_private_network: bool = False

    async def check(self, url: str) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            raise BrowserPolicyError("browser URLs must use HTTP, HTTPS, WS, or WSS")
        hostname = parsed.hostname
        if hostname is None:
            raise BrowserPolicyError("browser URL must include a hostname")
        normalized = hostname.rstrip(".").lower()
        if self.allowed_hosts and not any(
            _host_matches(normalized, pattern) for pattern in self.allowed_hosts
        ):
            raise BrowserPolicyError(f"host '{normalized}' is not in allowed_hosts")
        if self.allow_private_network:
            return
        addresses = await asyncio.to_thread(_resolve_addresses, normalized)
        if any(not address.is_global for address in addresses):
            raise BrowserPolicyError(f"host '{normalized}' resolves to a non-public address")


class PlaywrightBrowserConfig(BaseModel):
    """Launch and output settings for one Browser resource."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    browser_type: Literal["chromium", "firefox", "webkit"] = "chromium"
    headless: bool = True
    channel: str | None = None
    executable_path: str | None = None
    viewport_width: int = Field(default=1280, ge=320, le=7680)
    viewport_height: int = Field(default=720, ge=240, le=4320)
    action_timeout_ms: float = Field(default=15_000, gt=0, le=120_000)
    navigation_timeout_ms: float = Field(default=30_000, gt=0, le=180_000)
    max_snapshot_chars: int = Field(default=20_000, ge=100, le=1_000_000)
    network_policy: BrowserNetworkPolicy = Field(default_factory=BrowserNetworkPolicy)

    @model_validator(mode="after")
    def validate_executable_selection(self) -> PlaywrightBrowserConfig:
        if self.channel is not None and self.executable_path is not None:
            raise ValueError("channel and executable_path are mutually exclusive")
        if self.browser_type != "chromium" and self.channel is not None:
            raise ValueError("channel is only supported for chromium")
        return self


class PlaywrightBrowserSession:
    """One isolated BrowserContext and active Page."""

    def __init__(
        self,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        config: PlaywrightBrowserConfig,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self.config = config
        self._closed = False

    @classmethod
    async def create(cls, config: PlaywrightBrowserConfig) -> PlaywrightBrowserSession:
        playwright = await async_playwright().start()
        browser_type = getattr(playwright, config.browser_type)
        try:
            browser = await browser_type.launch(
                headless=config.headless,
                channel=config.channel,
                executable_path=config.executable_path,
            )
            context = await browser.new_context(
                viewport={
                    "width": config.viewport_width,
                    "height": config.viewport_height,
                }
            )
            context.set_default_timeout(config.action_timeout_ms)
            context.set_default_navigation_timeout(config.navigation_timeout_ms)

            async def enforce_policy(route: Route) -> None:
                try:
                    await config.network_policy.check(route.request.url)
                except BrowserPolicyError:
                    await route.abort("blockedbyclient")
                else:
                    await route.continue_()

            await context.route("**/*", enforce_policy)
            page = await context.new_page()
        except BaseException:
            await playwright.stop()
            raise
        return cls(playwright, browser, context, page, config)

    async def navigate(self, url: str) -> BrowserActionResult:
        self._ensure_open()
        await self.config.network_policy.check(url)
        await self._page.goto(url, wait_until="domcontentloaded")
        return await self._action_result("navigated")

    async def snapshot(self) -> BrowserSnapshot:
        self._ensure_open()
        await self._validate_active_url()
        text = await self._page.locator("body").inner_text()
        limit = self.config.max_snapshot_chars
        return BrowserSnapshot(
            url=self._page.url,
            title=await self._page.title(),
            text=text[:limit],
            truncated=len(text) > limit,
        )

    async def click(self, selector: str) -> BrowserActionResult:
        self._ensure_open()
        await self._page.locator(selector).first.click()
        return await self._action_result(f"clicked {selector}")

    async def fill(self, selector: str, text: str) -> BrowserActionResult:
        self._ensure_open()
        await self._page.locator(selector).first.fill(text)
        return await self._action_result(f"filled {selector}")

    async def press(self, key: str) -> BrowserActionResult:
        self._ensure_open()
        await self._page.keyboard.press(key)
        return await self._action_result(f"pressed {key}")

    async def select_option(self, selector: str, value: str) -> BrowserActionResult:
        self._ensure_open()
        await self._page.locator(selector).first.select_option(value=value)
        return await self._action_result(f"selected {selector}")

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        self._ensure_open()
        await self._validate_active_url()
        return await self._page.screenshot(type="png", full_page=full_page)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._context.close()
        finally:
            try:
                await self._browser.close()
            finally:
                await self._playwright.stop()

    async def _action_result(self, detail: str) -> BrowserActionResult:
        await self._validate_active_url()
        return BrowserActionResult(
            url=self._page.url,
            title=await self._page.title(),
            detail=detail,
        )

    async def _validate_active_url(self) -> None:
        if self._page.url == "about:blank":
            return
        try:
            await self.config.network_policy.check(self._page.url)
        except BrowserPolicyError:
            await self._page.close()
            self._page = await self._context.new_page()
            raise

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Browser session is closed")


def playwright_browser_resource(
    config: PlaywrightBrowserConfig,
    *,
    name: str = "browser",
    eager: bool = False,
) -> ResourceSpec:
    """Create an execution-scoped ResourceSpec for Playwright."""

    @asynccontextmanager
    async def factory(context: RuntimeContext) -> AsyncIterator[PlaywrightBrowserSession]:
        del context
        session = await PlaywrightBrowserSession.create(config)
        try:
            yield session
        finally:
            await session.close()

    return ResourceSpec(name=name, factory=factory, eager=eager)


def _host_matches(hostname: str, pattern: str) -> bool:
    normalized = pattern.rstrip(".").lower()
    if normalized.startswith("*."):
        suffix = normalized[2:]
        return hostname.endswith(f".{suffix}") and hostname != suffix
    return hostname == normalized


def _resolve_addresses(hostname: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        return tuple(
            {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            }
        )
    except socket.gaierror as exc:
        raise BrowserPolicyError(f"could not resolve host '{hostname}'") from exc
