# tiny-mcp-server

**The fastest, smallest way to build Model Context Protocol (MCP) servers in Python. Zero dependencies. MIT.**

`tiny-mcp-server` is a single-file Python framework for building [Model Context Protocol](https://modelcontextprotocol.io) servers. No external dependencies, no boilerplate — just decorate a function with `@tool` and you're shipping an MCP server.

## Protocol support

Speaks the **2026-07-28** MCP revision:

- `server/discover` — advertises `supportedVersions`, `capabilities`, identity
- Every request may carry its protocol version in `_meta`; an unknown version
  returns `-32022` with the list of supported versions
- Every result carries `resultType` and the server identity in `_meta`
- `tools/list` and `server/discover` carry `ttlMs` + `cacheScope`

The legacy `initialize` handler is **kept** and still answers — every server
observed in the wild that speaks the current dialect also kept it, and it
costs nothing. A legacy client and a current client can both talk to the
same server.

## Features

- 🪶 **One-file, zero-dependency** — pure Python stdlib, drop it in any project
- 🪄 **`@tool` decorator** — turn any function into an MCP tool with one line
- 🚀 **Multiple transports** — `stdio` (for desktop MCP clients) and `sse` (for web/remote)
- � **Automatic JSON Schema generation** — type hints + docstrings become tool schemas
- �️ **Built-in logging and error handling** — never crash on a malformed payload
- �️ **MIT licensed** — use it anywhere

## Install

Pick whichever is easiest:

```bash
# Option 1 — pip (when published)
pip install tiny-mcp-server

# Option 2 — curl a single file and import it
curl -O https://raw.githubusercontent.com/hussain-alsaibai/tiny-mcp-server/main/tiny_mcp_server.py

# Option 3 — vendor it
cp tiny_mcp_server.py your_project/
```

## Quick Start

Here's a complete MCP server with two tools — under 20 lines:

```python
from tiny_mcp_server import Server, tool

app = Server("hello-world")

@app.tool
def add(a: int, b: int) -> int:
    """Add two numbers together."""
    return a + b

@app.tool
def greet(name: str = "World") -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"

if __name__ == "__main__":
    app.run("stdio")  # or "sse" for HTTP transport
```

The `add` tool's auto-generated JSON Schema:

```json
{
  "name": "add",
  "description": "Add two numbers together.",
  "inputSchema": {
    "type": "object",
    "properties": {
      "a": {"type": "integer"},
      "b": {"type": "integer"}
    },
    "required": ["a", "b"]
  }
}
```

## Transports

### stdio (default — for Claude Desktop, Cursor, etc.)

```python
app.run("stdio")
```

The server reads JSON-RPC messages from stdin and writes responses to stdout. This is the transport used by every desktop MCP client.

### SSE (HTTP — for web clients)

```python
app.run("sse", host="127.0.0.1", port=8000)
```

Exposes a Server-Sent Events endpoint at `/sse` and a POST endpoint at `/messages`. Great for running an MCP server as a local microservice.

## API Reference

### `Server(name: str)`

```python
app = Server("my-server")
```

### `@app.tool`

```python
@app.tool(name="optional-name", description="optional-desc")
def my_tool(arg: str, count: int = 1) -> str:
    """Tool description (used as fallback if description not provided)."""
    return arg * count
```

- `name` — defaults to the function name
- `description` — defaults to the function's docstring
- Arguments are inferred from the signature; types from annotations
- Default values become optional in the schema

### `app.run(transport: str, **kwargs)`

- `"stdio"` — no extra args
- `"sse"` — `host` (default `127.0.0.1`), `port` (default `8000`)

## Examples

See [`examples/hello_world.py`](examples/hello_world.py) for a runnable demo.

## License

MIT © hussain-alsaibai
