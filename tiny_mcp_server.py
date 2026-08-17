"""
tiny_mcp_server — A zero-dependency Python framework for building MCP servers.

The Model Context Protocol (MCP) uses JSON-RPC 2.0 over a transport (stdio or SSE).
This module implements just enough of the spec to register tools, advertise them,
and dispatch `tools/call` requests. No third-party dependencies required.

Usage:
    from tiny_mcp_server import Server

    app = Server("my-server")

    @app.tool
    def add(a: int, b: int) -> int:
        \"\"\"Add two numbers.\"\"\"
        return a + b

    if __name__ == "__main__":
        app.run("stdio")
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import threading
import traceback
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
    get_args,
    get_origin,
)

__all__ = ["Server", "tool", "Tool", "ToolError", "StdioTransport", "SseTransport"]

__version__ = "0.1.0"

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

log = logging.getLogger("tiny_mcp_server")
if not log.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[tiny-mcp] %(levelname)s %(message)s"))
    log.addHandler(handler)
log.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Type hints → JSON Schema
# --------------------------------------------------------------------------- #

_PRIMITIVE_MAP: Dict[Any, str] = {
    int: "integer",
    float: "number",
    bool: "boolean",
    str: "string",
    bytes: "string",
    type(None): "null",
}


def _python_type_to_json_schema(annotation: Any) -> Dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema fragment."""
    if annotation is inspect.Parameter.empty or annotation is None:
        return {"type": "string"}

    # Handle Optional[X] / Union[X, None] / X | None
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and len(args) == 2:
            # Optional[X]
            return _python_type_to_json_schema(non_none[0])
        # Multi-type union → use anyOf
        return {"anyOf": [_python_type_to_json_schema(a) for a in non_none]}

    if origin in (list, List, Sequence, tuple, Tuple):
        if args:
            return {"type": "array", "items": _python_type_to_json_schema(args[0])}
        return {"type": "array"}

    if origin in (dict, Dict, Mapping):
        return {"type": "object"}

    if annotation in _PRIMITIVE_MAP:
        return {"type": _PRIMITIVE_MAP[annotation]}

    # Unknown type — fall back to string with no validation
    return {"type": "string"}


# --------------------------------------------------------------------------- #
# Tool definition
# --------------------------------------------------------------------------- #

@dataclass
class Tool:
    """A registered MCP tool.

    Generated automatically by the `@Server.tool` decorator; you usually don't
    construct this by hand.
    """

    name: str
    description: str
    func: Callable[..., Any]
    input_schema: Dict[str, Any] = field(default_factory=dict)


class ToolError(Exception):
    """Raised by a tool to signal an error to the MCP client.

    The message is returned in the JSON-RPC error response.
    """


# --------------------------------------------------------------------------- #
# Schema generation from a function signature
# --------------------------------------------------------------------------- #

def _build_input_schema(func: Callable[..., Any]) -> Dict[str, Any]:
    """Build a JSON Schema (draft-07-ish) from a function's signature."""
    sig = inspect.signature(func)
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for param_name, param in sig.parameters.items():
        if param_name == "self":
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            # *args / **kwargs aren't representable in MCP input schemas
            continue

        annotation = param.annotation
        schema = _python_type_to_json_schema(annotation)

        if param.default is not inspect.Parameter.empty:
            schema["default"] = param.default
        else:
            required.append(param_name)

        properties[param_name] = schema

    out: Dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        out["required"] = required
    return out


def _tool_description(func: Callable[..., Any], explicit: Optional[str]) -> str:
    if explicit:
        return explicit.strip()
    doc = inspect.getdoc(func)
    if doc:
        # Use first paragraph / first line of the docstring.
        return doc.strip().split("\n\n", 1)[0].strip()
    return func.__name__.replace("_", " ")


# --------------------------------------------------------------------------- #
# JSON-RPC plumbing
# --------------------------------------------------------------------------- #

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32653


def _make_result(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _make_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name)
    except (AttributeError, TypeError):
        return default


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

class Server:
    """A tiny MCP server.

    >>> app = Server("demo")
    >>> @app.tool
    ... def echo(msg: str) -> str:
    ...     '''Echo a message back.'''
    ...     return msg
    >>> app._tools["echo"].name
    'echo'
    """

    def __init__(
        self,
        name: str,
        version: str = "0.1.0",
        instructions: Optional[str] = None,
    ) -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self._tools: Dict[str, Tool] = {}
        self._lock = threading.RLock()

    # ---- public API ------------------------------------------------------- #

    def tool(
        self,
        func: Optional[Callable[..., Any]] = None,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
    ) -> Callable[..., Any]:
        """Register a function as an MCP tool.

        Used as a decorator with or without arguments:

            @app.tool
            def foo(): ...

            @app.tool(name="bar", description="Custom description")
            def foo(): ...
        """
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            tool_name = name or fn.__name__
            tool_desc = _tool_description(fn, description)
            input_schema = _build_input_schema(fn)
            with self._lock:
                if tool_name in self._tools:
                    raise ValueError(f"Tool {tool_name!r} already registered")
                self._tools[tool_name] = Tool(
                    name=tool_name,
                    description=tool_desc,
                    func=fn,
                    input_schema=input_schema,
                )
            log.info("registered tool %s", tool_name)
            return fn

        # Support bare `@app.tool` (without parens).
        if func is not None and callable(func) and not (
            isinstance(func, str) or name is not None or description is not None
        ):
            return decorator(func)
        return decorator

    def register(self, tool_obj: Tool) -> None:
        """Register a pre-built Tool object directly."""
        with self._lock:
            if tool_obj.name in self._tools:
                raise ValueError(f"Tool {tool_obj.name!r} already registered")
            self._tools[tool_obj.name] = tool_obj

    def run(self, transport: str = "stdio", **kwargs: Any) -> None:
        """Run the server with the requested transport.

        - ``"stdio"`` reads JSON-RPC messages from stdin (one per line or
          newline-delimited) and writes responses to stdout.
        - ``"sse"`` starts a small HTTP server with ``/sse`` (GET) for the event
          stream and ``/messages`` (POST) for client→server requests.
        """
        transport = transport.lower().strip()
        if transport in ("stdio", "stdin", "pipe"):
            StdioTransport(self).serve()
        elif transport in ("sse", "http", "server-sent-events"):
            host = kwargs.get("host", "127.0.0.1")
            port = int(kwargs.get("port", 8000))
            SseTransport(self, host=host, port=port).serve()
        else:
            raise ValueError(f"Unknown transport: {transport!r}")

    # ---- JSON-RPC dispatch ------------------------------------------------ #

    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process a single decoded JSON-RPC message.

        Returns a JSON-RPC response dict, or ``None`` for notifications.
        """
        req_id = message.get("id")

        if message.get("jsonrpc") != "2.0":
            return _make_error(req_id, INVALID_REQUEST, "jsonrpc must be '2.0'")

        method = message.get("method")
        if not isinstance(method, str):
            return _make_error(req_id, INVALID_REQUEST, "method must be a string")

        handler_name = "_handle_" + method.replace("/", "_")
        handler = getattr(self, handler_name, None)
        if handler is None:
            return _make_error(req_id, METHOD_NOT_FOUND, f"method not found: {method}")

        params = message.get("params") or {}
        try:
            result = handler(params)
        except ToolError as exc:
            return _make_error(req_id, INTERNAL_ERROR, str(exc))
        except TypeError as exc:
            log.warning("invalid params for %s: %s", method, exc)
            return _make_error(req_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface anything to client
            log.exception("internal error in %s", method)
            return _make_error(
                req_id,
                INTERNAL_ERROR,
                "internal error: " + str(exc),
                data={"traceback": traceback.format_exc(limit=5)},
            )

        # Notifications have no id; don't respond.
        if req_id is None and "id" not in message:
            return None
        return _make_result(req_id, result)

    # ---- MCP-specific handlers -------------------------------------------- #

    def _handle_initialize(self, params: Dict[str, Any]) -> Dict[str, Any]:
        client = _safe_getattr(params, "get", lambda *_: None)("clientInfo", {}) \
            if hasattr(params, "get") else params.get("clientInfo", {}) if isinstance(params, dict) else {}
        log.info(
            "initialize from client=%s version=%s",
            (client or {}).get("name", "?"),
            (client or {}).get("version", "?"),
        )
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }

    def _handle_ping(self, params: Dict[str, Any]) -> Dict[str, Any]:
        return {}

    def _handle_tools_list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            tools = [
                {
                    "name": t.name,
                    "description": t.description,
                    "inputSchema": t.input_schema,
                }
                for t in self._tools.values()
            ]
        return {"tools": tools}

    def _handle_tools_call(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(params, dict):
            raise ToolError("params must be an object")
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            raise ToolError("'name' must be a string")
        if not isinstance(arguments, dict):
            raise ToolError("'arguments' must be an object")

        with self._lock:
            tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"unknown tool: {name}")

        log.info("calling tool %s args=%s", name, arguments)
        try:
            result = tool.func(**arguments)
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s raised", name)
            return {
                "content": [{"type": "text", "text": f"Error in {name}: {exc}"}],
                "isError": True,
            }

        # Wrap non-text results as JSON.
        if isinstance(result, str):
            content = [{"type": "text", "text": result}]
        else:
            content = [{"type": "text", "text": json.dumps(result, default=str)}]

        return {"content": content, "isError": False}


# Module-level decorator shortcut for users who prefer `@tool` over `@app.tool`.
_default_server: Optional[Server] = None


def tool(
    func: Optional[Callable[..., Any]] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> Callable[..., Any]:
    """Convenience decorator that uses a process-wide default Server."""
    global _default_server
    if _default_server is None:
        _default_server = Server("default")
    return _default_server.tool(func, name=name, description=description)


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #

class StdioTransport:
    """Read newline-delimited JSON-RPC from stdin, write to stdout."""

    def __init__(self, server: Server) -> None:
        self.server = server

    def serve(self) -> None:
        log.info("stdio transport starting (server=%s)", self.server.name)
        stdin = sys.stdin
        stdout = sys.stdout
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                log.warning("invalid JSON on stdin: %s", exc)
                err = _make_error(None, PARSE_ERROR, "parse error", data=str(exc))
                stdout.write(json.dumps(err) + "\n")
                stdout.flush()
                continue

            if isinstance(message, list):
                # Batch request — handle each element.
                responses = []
                for item in message:
                    if not isinstance(item, dict):
                        responses.append(_make_error(None, INVALID_REQUEST, "bad item"))
                        continue
                    resp = self.server.handle_message(item)
                    if resp is not None:
                        responses.append(resp)
                if responses:
                    stdout.write(json.dumps(responses) + "\n")
                    stdout.flush()
            else:
                resp = self.server.handle_message(message)
                if resp is not None:
                    stdout.write(json.dumps(resp) + "\n")
                    stdout.flush()


# --------------------------------------------------------------------------- #
# SSE transport (zero-dep: uses wsgiref + a tiny threaded HTTP server)
# --------------------------------------------------------------------------- #

class SseTransport:
    """Server-Sent Events transport built on Python's stdlib HTTP server.

    This is intentionally minimal — just enough for a local-only MCP server
    that a browser-based client can talk to. Endpoints:

        GET  /sse       — opens an SSE stream; the server pushes ``endpoint``
                          and ``message`` events to the client.
        POST /messages  — accepts a JSON-RPC request, dispatches it, and
                          forwards the response on the SSE stream.
        GET  /          — a tiny landing page for sanity-checking.

    No external HTTP frameworks are required.
    """

    def __init__(self, server: Server, host: str = "127.0.0.1", port: int = 8000) -> None:
        self.server = server
        self.host = host
        self.port = port
        self._clients: List["SseClient"] = []
        self._lock = threading.Lock()

    def serve(self) -> None:
        import http.server
        import socketserver

        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
                log.info("http %s - " + fmt, self.address_string(), *args)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/sse"):
                    outer._handle_sse(self)
                elif self.path == "/" or self.path == "/index.html":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    body = (
                        "<h1>tiny-mcp-server</h1>"
                        "<p>Server: " + outer.server.name + "</p>"
                        "<ul>"
                        "<li>GET /sse — SSE event stream</li>"
                        "<li>POST /messages — JSON-RPC endpoint</li>"
                        "</ul>"
                    ).encode("utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404, "Not Found")

            def do_POST(self) -> None:  # noqa: N802
                if self.path.startswith("/messages"):
                    outer._handle_messages(self)
                else:
                    self.send_error(404, "Not Found")

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        httpd = ThreadedServer((self.host, self.port), Handler)
        log.info("SSE transport listening on http://%s:%d", self.host, self.port)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("SSE transport shutting down")
        finally:
            httpd.server_close()

    # -- internal handlers -------------------------------------------------- #

    def _handle_sse(self, handler: Any) -> None:
        client = SseClient(handler)
        with self._lock:
            self._clients.append(client)
        try:
            client.run()
        finally:
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)

    def _handle_messages(self, handler: Any) -> None:
        length = int(handler.headers.get("Content-Length", "0") or 0)
        body = handler.rfile.read(length) if length else b""
        try:
            message = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError as exc:
            handler.send_response(400)
            handler.send_header("Content-Type", "application/json")
            err = _make_error(None, PARSE_ERROR, "parse error", data=str(exc))
            out = json.dumps(err).encode("utf-8")
            handler.send_header("Content-Length", str(len(out)))
            handler.end_headers()
            handler.wfile.write(out)
            return

        response = self.server.handle_message(message)

        # Always 202 — actual response goes out via the SSE stream.
        handler.send_response(202)
        handler.send_header("Content-Type", "application/json")
        payload = json.dumps({"status": "queued" if response is None else "ok"}).encode("utf-8")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)

        if response is not None:
            self._broadcast(response)

    def _broadcast(self, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.send_message(data)


class SseClient:
    """One connected SSE client."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self._queue: List[str] = []
        self._cv = threading.Condition()

    def run(self) -> None:
        handler = self.handler
        try:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/event-stream")
            handler.send_header("Cache-Control", "no-cache")
            handler.send_header("Connection", "keep-alive")
            handler.end_headers()

            # Tell the client where to POST messages.
            self._emit("endpoint", "/messages")

            # Periodically send heartbeats so the connection stays open.
            import time
            last_beat = time.time()
            handler.connection.setblocking(False) if hasattr(handler.connection, "setblocking") else None
            while True:
                with self._cv:
                    self._cv.wait(timeout=15.0)
                    items = self._queue[:]
                    self._queue.clear()
                for data in items:
                    self._emit_raw(data)
                now = time.time()
                if now - last_beat >= 15:
                    self._emit_comment("keep-alive")
                    last_beat = now
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def send_message(self, data: str) -> None:
        with self._cv:
            self._queue.append(data)
            self._cv.notify_all()

    def _emit(self, event: str, data: str) -> None:
        try:
            self.handler.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
            self.handler.wfile.flush()
        except OSError:
            raise

    def _emit_raw(self, data: str) -> None:
        try:
            self.handler.wfile.write(f"event: message\ndata: {data}\n\n".encode("utf-8"))
            self.handler.wfile.flush()
        except OSError:
            raise

    def _emit_comment(self, text: str) -> None:
        try:
            self.handler.wfile.write(f": {text}\n\n".encode("utf-8"))
            self.handler.wfile.flush()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    app = Server("tiny-mcp-server-demo")

    @app.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @app.tool
    def greet(name: str = "World") -> str:
        """Say hello."""
        return f"Hello, {name}!"

    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    app.run(transport)
