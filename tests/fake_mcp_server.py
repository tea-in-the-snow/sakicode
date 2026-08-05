"""A fake MCP server over stdio, driven by tests/test_mcp.py.

Speaks newline-delimited JSON-RPC 2.0. Tools:
- echo: returns the given message;
- always_error: returns an isError result;
- rpc_error: answers with a JSON-RPC error object instead of a result;
- sleep: sleeps for `seconds` before answering (timeout tests);
- crash: exits the process immediately (fault-isolation tests).

Set FAKE_MCP_GARBAGE=1 to make the server answer `initialize` with invalid
JSON. Before every tools/call result the server emits a notification so the
client's framing loop is exercised against interleaved messages.
"""

import json
import os
import sys
import time

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "echo",
        "description": "Return the given message.",
        "inputSchema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "always_error",
        "description": "Always return an isError result.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "rpc_error",
        "description": "Answer with a JSON-RPC error object.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sleep",
        "description": "Sleep for `seconds` before answering.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
    },
    {
        "name": "crash",
        "description": "Terminate the server process immediately.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def handle_call(request):
    params = request.get("params") or {}
    name = params.get("name")
    arguments = params.get("arguments") or {}
    # Interleave a notification before the response: the client must skip it.
    send(
        {
            "jsonrpc": "2.0",
            "method": "notifications/message",
            "params": {"level": "info", "data": f"calling {name}"},
        }
    )
    if name == "crash":
        os._exit(1)
    if name == "rpc_error":
        send(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32601, "message": "tool disabled by server"},
            }
        )
        return
    if name == "sleep":
        time.sleep(arguments.get("seconds", 5))
        result = {"content": [{"type": "text", "text": "slept"}], "isError": False}
    elif name == "always_error":
        result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
    else:  # echo
        result = {
            "content": [{"type": "text", "text": arguments.get("message", "")}],
            "isError": False,
        }
    send({"jsonrpc": "2.0", "id": request["id"], "result": result})


def main():
    garbage = os.environ.get("FAKE_MCP_GARBAGE") == "1"
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            if garbage:
                sys.stdout.write("this is not json\n")
                sys.stdout.flush()
                continue
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-mcp", "version": "0.0.1"},
                    },
                }
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": TOOLS}})
        elif method == "tools/call":
            handle_call(request)


if __name__ == "__main__":
    main()
