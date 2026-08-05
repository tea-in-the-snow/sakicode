"""Tests for the MCP client: handshake, discovery, calls, and fault isolation."""

import json
import sys
from pathlib import Path

import pytest

from sakicode.mcp import (
    McpConfigError,
    McpError,
    McpProtocolError,
    McpServerSpec,
    StdioMcpClient,
    connect,
    connect_configured_servers,
    load_server_specs,
)
from sakicode.permissions import Decision, PermissionEngine
from sakicode.tooling import ToolErrorCode, ToolRegistry

FAKE_SERVER = Path(__file__).parent / "fake_mcp_server.py"


def make_spec(name="fake", timeout=5.0, env=None):
    return McpServerSpec(
        name=name,
        command=[sys.executable, str(FAKE_SERVER)],
        request_timeout=timeout,
        env=env,
    )


@pytest.fixture
def registry():
    return ToolRegistry()


@pytest.fixture
def client(registry):
    connected = connect(make_spec(), registry)
    yield connected
    connected.close()


def test_handshake_discovers_and_registers_tools(registry, client):
    assert client.server_info["name"] == "fake-mcp"
    tool = registry.get("mcp__fake__echo")
    assert tool is not None
    assert tool.description.startswith("[MCP fake]")
    schemas = {schema["function"]["name"] for schema in registry.schemas()}
    assert {"mcp__fake__echo", "mcp__fake__always_error"} <= schemas


def test_call_tool_round_trip_through_registry(registry, client):
    result = registry.execute("mcp__fake__echo", {"message": "hello mcp"})

    assert not result.is_error
    assert result.content == "hello mcp"
    assert result.metadata["server"] == "fake"
    assert result.metadata["remote_tool"] == "echo"
    trace = registry.traces[-1]
    assert trace.tool_name == "mcp__fake__echo"
    assert trace.ok


def test_arguments_are_validated_against_the_remote_schema(registry, client):
    result = registry.execute("mcp__fake__echo", {"message": 42})

    assert result.is_error
    assert result.error_code is ToolErrorCode.INVALID_ARGUMENTS
    assert "message" in result.content


def test_remote_is_error_result_becomes_structured_error(registry, client):
    result = registry.execute("mcp__fake__always_error", {})

    assert result.is_error
    assert result.error_code is ToolErrorCode.EXECUTION_ERROR
    assert "boom" in result.content


def test_json_rpc_error_response_becomes_structured_error(registry, client):
    result = registry.execute("mcp__fake__rpc_error", {})

    assert result.is_error
    assert result.error_code is ToolErrorCode.EXECUTION_ERROR
    assert "tool disabled by server" in result.content
    # A JSON-RPC error does not poison the connection: the next call works.
    follow_up = registry.execute("mcp__fake__echo", {"message": "still alive"})
    assert not follow_up.is_error


def test_timeout_kills_the_server_and_breaks_the_client(registry):
    client = connect(make_spec(timeout=0.3), registry)
    try:
        result = registry.execute("mcp__fake__sleep", {"seconds": 30})

        assert result.is_error
        assert result.error_code is ToolErrorCode.TIMEOUT
        assert not client.is_alive
        # A broken client fails fast instead of hanging the agent again.
        follow_up = registry.execute("mcp__fake__echo", {"message": "hi"})
        assert follow_up.is_error
        assert follow_up.error_code is ToolErrorCode.EXECUTION_ERROR
        assert "broken" in follow_up.content
    finally:
        client.close()


def test_server_crash_is_contained(registry, client):
    result = registry.execute("mcp__fake__crash", {})

    assert result.is_error
    assert result.error_code is ToolErrorCode.EXECUTION_ERROR
    assert not client.is_alive
    follow_up = registry.execute("mcp__fake__echo", {"message": "hi"})
    assert follow_up.is_error
    assert "broken" in follow_up.content


def test_garbage_handshake_fails_connect_and_cleans_up(registry):
    with pytest.raises(McpProtocolError, match="invalid JSON"):
        connect(make_spec(env={"FAKE_MCP_GARBAGE": "1"}), registry)
    assert registry.get("mcp__fake__echo") is None


def test_unstartable_server_raises_process_error(registry, tmp_path):
    spec = McpServerSpec(
        name="missing", command=[str(tmp_path / "no-such-binary")]
    )
    with pytest.raises(McpError, match="cannot start"):
        connect(spec, registry)


def test_permission_engine_asks_for_mcp_tools_with_session_grant(tmp_path):
    engine = PermissionEngine(tmp_path)

    decision = engine.evaluate("mcp__fake__echo", {"message": "hi"})
    assert decision.decision is Decision.ASK
    assert decision.grant_key == "mcp:mcp__fake__echo"
    assert decision.session_grantable

    engine.approve_session(decision)
    granted = engine.evaluate("mcp__fake__echo", {"message": "hi"})
    assert granted.decision is Decision.ALLOW
    # The grant covers this exact tool only, not every MCP tool.
    other = engine.evaluate("mcp__fake__always_error", {})
    assert other.decision is Decision.ASK


def test_load_server_specs_round_trip(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "docs",
                        "command": ["python", "server.py"],
                        "request_timeout": 3,
                        "env": {"MODE": "test"},
                    }
                ]
            }
        )
    )

    specs = load_server_specs(config)
    assert specs == [
        McpServerSpec(
            name="docs",
            command=["python", "server.py"],
            request_timeout=3.0,
            env={"MODE": "test"},
        )
    ]


def test_load_server_specs_rejects_malformed_entries(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"servers": [{"name": "bad name!"}]}))

    with pytest.raises(McpConfigError, match=r"servers\[0\]"):
        load_server_specs(config)


def test_connect_configured_servers_skips_broken_servers(registry, tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "broken", "command": ["/nonexistent/binary"]},
                    {
                        "name": "fake",
                        "command": [sys.executable, str(FAKE_SERVER)],
                    },
                ]
            }
        )
    )
    errors = []

    clients = connect_configured_servers(config, registry, on_error=errors.append)
    try:
        assert len(clients) == 1
        assert len(errors) == 1 and "broken" in errors[0]
        assert registry.get("mcp__fake__echo") is not None
    finally:
        for client in clients:
            client.close()


def test_missing_config_file_means_no_servers(registry, tmp_path):
    clients = connect_configured_servers(tmp_path / "absent.json", registry)
    assert clients == []


def test_stdio_client_rejects_foreign_response_ids(registry):
    client = StdioMcpClient(make_spec())
    # Not started: any request must fail cleanly rather than hang.
    with pytest.raises(McpError, match="broken|not been started"):
        client.list_tools()
