"""
hello_world.py — A minimal MCP server built with tiny-mcp-server.

Run it with one of:

    python hello_world.py            # stdio transport (default)
    python hello_world.py sse        # SSE transport on http://127.0.0.1:8000

Then point any MCP-compatible client (Claude Desktop, Cursor, etc.) at it.
"""

from tiny_mcp_server import Server

app = Server(
    name="hello-world",
    version="0.1.0",
    instructions="A friendly demo MCP server with two trivial tools.",
)


@app.tool
def add(a: int, b: int) -> int:
    """Add two integers and return the result.

    Examples: add(1, 2) -> 3
    """
    return a + b


@app.tool
def greet(name: str = "World", punctuation: str = "!") -> str:
    """Greet someone by name with an optional punctuation mark."""
    return f"Hello, {name}{punctuation}"


@app.tool(description="Reverse a string.")
def reverse(text: str) -> str:
    # Description is supplied explicitly; no docstring needed.
    return text[::-1]


if __name__ == "__main__":
    import sys

    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    if transport == "sse":
        app.run("sse", host="127.0.0.1", port=8000)
    else:
        app.run("stdio")
