"""Resource-aware Tools for any BrowserSession implementation."""

from base_agent.browser.protocol import BrowserSession
from base_agent.tools import FunctionTool, ToolContext, tool


def browser_tools(*, resource_name: str = "browser") -> tuple[FunctionTool, ...]:
    """Build a conservative Browser Tool set without arbitrary JavaScript execution."""

    @tool(name="browser_navigate", permissions=frozenset({"browser:navigate"}))
    async def navigate(url: str, context: ToolContext) -> dict[str, object]:
        """Navigate the isolated browser page to an allowed HTTP or HTTPS URL."""
        browser = await context.resources.get(resource_name, BrowserSession)
        return (await browser.navigate(url)).model_dump(mode="json")

    @tool(name="browser_snapshot", permissions=frozenset({"browser:read"}))
    async def snapshot(context: ToolContext) -> dict[str, object]:
        """Read the active page URL, title, and bounded visible text."""
        browser = await context.resources.get(resource_name, BrowserSession)
        return (await browser.snapshot()).model_dump(mode="json")

    @tool(name="browser_click", permissions=frozenset({"browser:interact"}))
    async def click(selector: str, context: ToolContext) -> dict[str, object]:
        """Click the first page element matching a Playwright selector."""
        browser = await context.resources.get(resource_name, BrowserSession)
        return (await browser.click(selector)).model_dump(mode="json")

    @tool(name="browser_fill", permissions=frozenset({"browser:interact"}))
    async def fill(selector: str, text: str, context: ToolContext) -> dict[str, object]:
        """Replace the value of an input matching a Playwright selector."""
        browser = await context.resources.get(resource_name, BrowserSession)
        return (await browser.fill(selector, text)).model_dump(mode="json")

    @tool(name="browser_press", permissions=frozenset({"browser:interact"}))
    async def press(key: str, context: ToolContext) -> dict[str, object]:
        """Press a keyboard key on the active page."""
        browser = await context.resources.get(resource_name, BrowserSession)
        return (await browser.press(key)).model_dump(mode="json")

    @tool(name="browser_select_option", permissions=frozenset({"browser:interact"}))
    async def select_option(
        selector: str,
        value: str,
        context: ToolContext,
    ) -> dict[str, object]:
        """Select one value in a matching HTML select element."""
        browser = await context.resources.get(resource_name, BrowserSession)
        return (await browser.select_option(selector, value)).model_dump(mode="json")

    @tool(name="browser_screenshot", permissions=frozenset({"browser:capture"}))
    async def screenshot(
        context: ToolContext,
        name: str = "browser-screenshot.png",
        full_page: bool = False,
    ) -> dict[str, object]:
        """Capture the page into a Run-owned PNG Artifact."""
        browser = await context.resources.get(resource_name, BrowserSession)
        content = await browser.screenshot(full_page=full_page)
        artifact = await context.artifacts.create(
            name=name,
            media_type="image/png",
            content=content,
            metadata={"source": "browser", "full_page": full_page},
        )
        return artifact.model_dump(mode="json")

    return navigate, snapshot, click, fill, press, select_option, screenshot
