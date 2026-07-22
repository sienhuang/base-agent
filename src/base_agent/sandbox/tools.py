"""Resource-aware Tools for any SandboxSession implementation."""

from base_agent.sandbox.protocol import SandboxSession
from base_agent.tools import FunctionTool, ToolContext, tool


def sandbox_tools(
    *,
    resource_name: str = "sandbox",
    execute_timeout_seconds: float = 65.0,
) -> tuple[FunctionTool, ...]:
    """Build generic Sandbox tools bound to one execution-scoped resource name."""

    @tool(
        name="sandbox_execute",
        permissions=frozenset({"sandbox:execute"}),
        timeout_seconds=execute_timeout_seconds,
    )
    async def execute(
        argv: list[str],
        context: ToolContext,
        cwd: str = ".",
    ) -> dict[str, object]:
        """Execute an argv array inside the isolated workspace; no shell is implied."""
        session = await context.resources.get(resource_name, SandboxSession)
        result = await session.execute(argv, cwd=cwd)
        return result.model_dump(mode="json")

    @tool(
        name="sandbox_read_text",
        permissions=frozenset({"sandbox:read"}),
    )
    async def read_text(path: str, context: ToolContext) -> dict[str, object]:
        """Read a bounded UTF-8 text file from the isolated workspace."""
        session = await context.resources.get(resource_name, SandboxSession)
        result = await session.read_text(path)
        return result.model_dump(mode="json")

    @tool(
        name="sandbox_write_text",
        permissions=frozenset({"sandbox:write"}),
    )
    async def write_text(
        path: str,
        content: str,
        context: ToolContext,
        append: bool = False,
    ) -> dict[str, object]:
        """Write UTF-8 text inside the isolated workspace."""
        session = await context.resources.get(resource_name, SandboxSession)
        await session.write_text(path, content, append=append)
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    return execute, read_text, write_text
