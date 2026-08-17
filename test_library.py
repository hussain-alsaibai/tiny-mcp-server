"""Self-test for tiny_mcp_server — exercises every code path without any I/O.

Run:  python3 test_library.py
"""

import json
import sys
import traceback

sys.path.insert(0, ".")

from tiny_mcp_server import Server, Tool, ToolError, _build_input_schema


def assert_eq(actual, expected, msg):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def assert_in(needle, haystack, msg):
    if needle not in haystack:
        raise AssertionError(f"{msg}: {needle!r} not in {haystack!r}")


def test_schema_generation():
    print("[test] schema generation ...", end=" ")

    def f(a: int, b: str = "x", c: float = 1.0, d: bool = False) -> int:
        return a + len(b)

    schema = _build_input_schema(f)
    assert_eq(schema["type"], "object", "schema type")
    assert_in("a", schema["properties"], "missing 'a'")
    assert_eq(schema["properties"]["a"], {"type": "integer"}, "a schema")
    assert_eq(schema["properties"]["b"], {"type": "string", "default": "x"}, "b schema")
    assert_eq(schema["required"], ["a"], "required list")
    print("ok")


def test_optional_and_list():
    print("[test] Optional / List annotations ...", end=" ")
    from typing import List, Optional

    def g(items: List[int], maybe: Optional[str] = None) -> int:
        return sum(items)

    schema = _build_input_schema(g)
    assert_eq(schema["properties"]["items"], {"type": "array", "items": {"type": "integer"}}, "list schema")
    # Optional[X] should unwrap to X with the default.
    assert_eq(schema["properties"]["maybe"], {"type": "string", "default": None}, "optional schema")
    print("ok")


def test_decorator_basic():
    print("[test] @app.tool decorator ...", end=" ")
    app = Server("test")

    @app.tool
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    assert "add" in app._tools, "tool not registered"
    tool = app._tools["add"]
    assert_eq(tool.name, "add", "tool name")
    assert_eq(tool.description, "Add two numbers.", "docstring → description")
    assert_eq(tool.input_schema["required"], ["a", "b"], "required fields")
    print("ok")


def test_decorator_with_args():
    print("[test] @app.tool(name=..., description=...) ...", end=" ")
    app = Server("test")

    @app.tool(name="renamed", description="Custom description.")
    def foo(x: str) -> str:
        return x

    assert "renamed" in app._tools, "renamed tool missing"
    assert "foo" not in app._tools, "original name should not be registered"
    assert_eq(app._tools["renamed"].description, "Custom description.", "explicit description")
    print("ok")


def test_initialize():
    print("[test] initialize handshake ...", end=" ")
    app = Server("hello", version="1.2.3")

    @app.tool
    def echo(msg: str) -> str:
        return msg

    resp = app.handle_message({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert_eq(resp["jsonrpc"], "2.0", "jsonrpc version")
    assert_eq(resp["id"], 1, "id")
    assert_eq(resp["result"]["serverInfo"]["name"], "hello", "server name")
    assert_eq(resp["result"]["serverInfo"]["version"], "1.2.3", "server version")
    assert_in("tools", resp["result"]["capabilities"], "capabilities")
    print("ok")


def test_tools_list():
    print("[test] tools/list ...", end=" ")
    app = Server("test")

    @app.tool
    def a(x: int) -> int:
        """Tool A."""
        return x

    @app.tool
    def b(y: str = "z") -> str:
        return y

    resp = app.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = resp["result"]["tools"]
    names = sorted(t["name"] for t in tools)
    assert_eq(names, ["a", "b"], "tool names")
    a_schema = next(t for t in tools if t["name"] == "a")
    assert_eq(a_schema["inputSchema"]["required"], ["x"], "required for a")
    print("ok")


def test_tools_call():
    print("[test] tools/call ...", end=" ")
    app = Server("test")

    @app.tool
    def add(a: int, b: int) -> int:
        return a + b

    @app.tool
    def fail() -> str:
        raise ToolError("intentional")

    resp = app.handle_message({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
    })
    assert_eq(resp["result"]["isError"], False, "no error")
    assert_eq(resp["result"]["content"][0]["text"], "5", "result text")

    # Unknown tool
    resp_err = app.handle_message({
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "nope", "arguments": {}},
    })
    assert_in("error", resp_err, "should error for unknown tool")

    # ToolError raised inside tool
    resp_tool_err = app.handle_message({
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "fail", "arguments": {}},
    })
    assert_in("error", resp_tool_err, "should error on ToolError")
    print("ok")


def test_invalid_jsonrpc():
    print("[test] invalid JSON-RPC ...", end=" ")
    app = Server("test")
    resp = app.handle_message({"jsonrpc": "1.0", "id": 1, "method": "ping"})
    assert_in("error", resp, "should error on bad jsonrpc version")
    assert_eq(resp["error"]["code"], -32600, "INVALID_REQUEST code")

    resp = app.handle_message({"jsonrpc": "2.0", "id": 1, "method": "does/not/exist"})
    assert_in("error", resp, "should error on unknown method")
    assert_eq(resp["error"]["code"], -32601, "METHOD_NOT_FOUND code")
    print("ok")


def test_notification_no_response():
    print("[test] notifications ...", end=" ")
    app = Server("test")
    resp = app.handle_message({"jsonrpc": "2.0", "method": "ping"})
    assert resp is None, "notification should yield no response"
    print("ok")


def test_example_module_imports():
    print("[test] examples/hello_world.py imports cleanly ...", end=" ")
    import importlib.util
    spec = importlib.util.spec_from_file_location("hello_world", "examples/hello_world.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = sorted(mod.app._tools.keys())
    assert_eq(names, ["add", "greet", "reverse"], "example tools")
    print("ok")


def main():
    tests = [
        test_schema_generation,
        test_optional_and_list,
        test_decorator_basic,
        test_decorator_with_args,
        test_initialize,
        test_tools_list,
        test_tools_call,
        test_invalid_jsonrpc,
        test_notification_no_response,
        test_example_module_imports,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception:  # noqa: BLE001
            failed += 1
            print("FAIL")
            traceback.print_exc()
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(tests)} tests passed.")


if __name__ == "__main__":
    main()
