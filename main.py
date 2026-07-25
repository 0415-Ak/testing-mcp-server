from fastmcp import FastMCP

mcp = FastMCP("simple-server")


@mcp.tool()
def ping() -> str:
    """Return a simple response to verify the server is running."""
    return "pong"


if __name__ == "__main__":
    mcp.run()
