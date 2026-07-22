from mcp.server.fastmcp import FastMCP

mcp = FastMCP("base-agent-test-server")


@mcp.tool()
def multiply(left: int, right: int) -> dict[str, int]:
    """Multiply two integers."""
    return {"product": left * right}


if __name__ == "__main__":
    mcp.run(transport="stdio")
