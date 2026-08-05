"""MCP client: stdio transport, JSON-RPC handshake, and registry integration.

An MCP server is an untrusted subprocess. This module keeps it at arm's
length: newline-delimited JSON-RPC over pipes, one outstanding request at a
time, a hard timeout on every request, and a kill switch that fires the
moment the framing becomes unrecoverable. Remote tools are adapted to the
same ``Tool`` protocol as built-ins, so schema validation, tracing, and the
permission engine apply unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
from typing import Any

from . import __version__
from .tooling import ToolErrorCode, ToolRegistry, ToolResult

PROTOCOL_VERSION = "2025-06-18"
DEFAULT_REQUEST_TIMEOUT = 10.0  # seconds
MAX_MESSAGE_BYTES = 10 * 1024 * 1024  # cap one inbound JSON-RPC message
TOOL_NAME_PREFIX = "mcp__"
DEFAULT_CONFIG_PATH = Path(".sakicode") / "mcp.json"


class McpError(Exception):
    """Base class for every MCP failure mode."""


class McpProtocolError(McpError):
    """The peer violated JSON-RPC framing or the MCP message contract."""


class McpRemoteError(McpError):
    """The server answered with a JSON-RPC error object (recoverable)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"JSON-RPC error {code}: {message}")
        self.code = code


class McpTimeoutError(McpError):
    """A request did not complete within the configured timeout."""


class McpProcessError(McpError):
    """The server process died, could not start, or is marked broken."""


class McpConfigError(McpError):
    """The MCP server configuration file is malformed."""


@dataclass(frozen=True)
class McpServerSpec:
    """How to launch one MCP server subprocess."""

    name: str
    command: list[str]
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    env: dict[str, str] | None = None


class StdioMcpClient:
    """Speak JSON-RPC 2.0 with one MCP server over its stdin/stdout pipes.

    The client is synchronous and allows exactly one outstanding request —
    matching how the agent calls tools sequentially. Any framing failure,
    timeout, EOF, or crash marks the client broken and kills the child: a
    poisoned stream cannot be resynchronized reliably, so it is abandoned
    instead of being trusted further.
    """

    def __init__(self, spec: McpServerSpec) -> None:
        self.spec = spec
        self.server_info: dict[str, Any] = {}
        self._process: subprocess.Popen[bytes] | None = None
        self._next_id = 1
        self._buffer = bytearray()
        self._broken_reason: str | None = None

    @property
    def is_alive(self) -> bool:
        """True while the child runs and the stream is still trusted."""
        return (
            self._broken_reason is None
            and self._process is not None
            and self._process.poll() is None
        )

    def start(self) -> None:
        """Spawn the server subprocess with pipes for stdio framing."""
        if self._process is not None:
            raise McpProcessError(f"server {self.spec.name!r} is already started")
        env = dict(os.environ)
        env.update(self.spec.env or {})
        try:
            self._process = subprocess.Popen(
                self.spec.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # inherit: server diagnostics stay visible
                env=env,
            )
        except OSError as error:
            raise McpProcessError(
                f"cannot start MCP server {self.spec.name!r}: {error}"
            ) from error

    def initialize(self) -> dict[str, Any]:
        """Run the MCP handshake: initialize request + initialized notification."""
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sakicode", "version": __version__},
            },
        )
        if not isinstance(result, dict) or "protocolVersion" not in result:
            self._break("initialize result is missing protocolVersion")
            raise McpProtocolError(
                f"server {self.spec.name!r} returned an invalid initialize result"
            )
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[dict[str, Any]]:
        """Discover remote tools; each entry is validated by the registry later."""
        result = self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            self._break("tools/list result is missing the tools array")
            raise McpProtocolError(
                f"server {self.spec.name!r} returned an invalid tools/list result"
            )
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke one remote tool and return the raw MCP result object."""
        result = self._request(
            "tools/call", {"name": name, "arguments": arguments}
        )
        if not isinstance(result, dict) or "content" not in result:
            self._break("tools/call result is missing the content array")
            raise McpProtocolError(
                f"server {self.spec.name!r} returned an invalid tools/call result"
            )
        return result

    def close(self) -> None:
        """Stop the child: close stdin, terminate, then kill if it lingers."""
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def __enter__(self) -> StdioMcpClient:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        """Send one request and wait for its matching response.

        Notifications arriving in between are skipped; a response carrying a
        foreign id is a protocol violation (this client never pipelines).
        """
        if self._broken_reason is not None:
            raise McpProcessError(
                f"MCP server {self.spec.name!r} is broken: {self._broken_reason}"
            )
        request_id = self._next_id
        self._next_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + self.spec.request_timeout
        try:
            while True:
                message = self._read_message(deadline)
                if "id" not in message or "method" in message:
                    continue  # server notification; not our response
                if message["id"] != request_id:
                    raise McpProtocolError(
                        f"response id {message['id']!r} does not match "
                        f"request id {request_id!r}"
                    )
                if "error" in message:
                    error = message["error"] or {}
                    raise McpRemoteError(
                        int(error.get("code", 0)),
                        str(error.get("message", "unknown remote error")),
                    )
                return message.get("result")
        except McpRemoteError:
            raise  # the server is healthy; only this call failed
        except McpError as error:
            self._break(str(error))
            raise

    def _send(self, message: dict[str, Any]) -> None:
        process = self._require_process()
        assert process.stdin is not None
        try:
            process.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            self._break(f"cannot write to server stdin: {error}")
            raise McpProcessError(
                f"MCP server {self.spec.name!r} is not writable: {error}"
            ) from error

    def _read_message(self, deadline: float) -> dict[str, Any]:
        """Read one newline-delimited JSON message, honoring the deadline."""
        process = self._require_process()
        assert process.stdout is not None
        fd = process.stdout.fileno()
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                return self._parse_message(line)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise McpTimeoutError(
                    f"server {self.spec.name!r} did not respond within "
                    f"{self.spec.request_timeout}s"
                )
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise McpTimeoutError(
                    f"server {self.spec.name!r} did not respond within "
                    f"{self.spec.request_timeout}s"
                )
            chunk = os.read(fd, 65536)
            if not chunk:
                raise McpProcessError(
                    f"server {self.spec.name!r} closed its stdout "
                    f"(exit code {process.poll()})"
                )
            self._buffer.extend(chunk)
            if len(self._buffer) > MAX_MESSAGE_BYTES:
                raise McpProtocolError(
                    f"server {self.spec.name!r} exceeded the "
                    f"{MAX_MESSAGE_BYTES}-byte message limit"
                )

    def _parse_message(self, line: bytes) -> dict[str, Any]:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise McpProtocolError(
                f"server {self.spec.name!r} sent invalid JSON: {error}"
            ) from error
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise McpProtocolError(
                f"server {self.spec.name!r} sent a non-JSON-RPC message"
            )
        return message

    def _require_process(self) -> subprocess.Popen[bytes]:
        if self._broken_reason is not None:
            raise McpProcessError(
                f"MCP server {self.spec.name!r} is broken: {self._broken_reason}"
            )
        if self._process is None:
            raise McpProcessError(
                f"MCP server {self.spec.name!r} has not been started"
            )
        return self._process

    def _break(self, reason: str) -> None:
        """Mark the stream unrecoverable and kill the child process."""
        self._broken_reason = reason
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass


@dataclass(frozen=True)
class McpTool:
    """Adapt one remote MCP tool to the shared Tool protocol."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    client: StdioMcpClient
    server_name: str
    remote_name: str
    requires_confirmation: bool = True

    def invoke(self, arguments: dict[str, Any]) -> ToolResult:
        try:
            result = self.client.call_tool(self.remote_name, arguments)
        except McpTimeoutError as error:
            return ToolResult.error(
                ToolErrorCode.TIMEOUT, str(error), server=self.server_name
            )
        except McpError as error:
            return ToolResult.error(
                ToolErrorCode.EXECUTION_ERROR, str(error), server=self.server_name
            )
        text = _content_to_text(result.get("content"))
        metadata = {"server": self.server_name, "remote_tool": self.remote_name}
        if result.get("isError"):
            return ToolResult.error(
                ToolErrorCode.EXECUTION_ERROR, text, **metadata
            )
        return ToolResult.success(text, **metadata)


def connect(spec: McpServerSpec, registry: ToolRegistry) -> StdioMcpClient:
    """Start a server, handshake, discover tools, and register them.

    On any failure the subprocess is torn down and the error propagates, so a
    half-connected server never leaks into the registry.
    """
    client = StdioMcpClient(spec)
    try:
        client.start()
        client.initialize()
        for tool_def in client.list_tools():
            registry.register(_to_mcp_tool(spec.name, tool_def, client))
    except Exception:
        client.close()
        raise
    return client


def connect_configured_servers(
    config_path: Path,
    registry: ToolRegistry,
    on_error: Callable[[str], None] | None = None,
) -> list[StdioMcpClient]:
    """Connect every server in the config file; failures skip one server.

    A missing config file means MCP is simply not configured. A broken server
    is reported through ``on_error`` and skipped so it cannot take down the
    whole agent at startup.
    """
    if not config_path.exists():
        return []
    clients = []
    for spec in load_server_specs(config_path):
        try:
            clients.append(connect(spec, registry))
        except McpError as error:
            if on_error is not None:
                on_error(str(error))
    return clients


def load_server_specs(path: Path) -> list[McpServerSpec]:
    """Parse and validate an MCP config file.

    Expected shape::

        {"servers": [{"name": "docs", "command": ["python", "server.py"],
                      "request_timeout": 10, "env": {"KEY": "value"}}]}
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise McpConfigError(f"cannot read MCP config {path}: {error}") from error
    servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(servers, list):
        raise McpConfigError(f"{path}: expected a 'servers' array")
    specs = []
    for index, entry in enumerate(servers):
        specs.append(_parse_server_entry(path, index, entry))
    return specs


def _parse_server_entry(path: Path, index: int, entry: Any) -> McpServerSpec:
    where = f"{path}: servers[{index}]"
    if not isinstance(entry, dict):
        raise McpConfigError(f"{where}: expected an object")
    name = entry.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise McpConfigError(
            f"{where}: 'name' must match [A-Za-z0-9_-]+, got {name!r}"
        )
    command = entry.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise McpConfigError(f"{where}: 'command' must be a non-empty string array")
    timeout = entry.get("request_timeout", DEFAULT_REQUEST_TIMEOUT)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise McpConfigError(f"{where}: 'request_timeout' must be a positive number")
    env = entry.get("env")
    if env is not None and (
        not isinstance(env, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
    ):
        raise McpConfigError(f"{where}: 'env' must be a string-to-string object")
    return McpServerSpec(
        name=name,
        command=list(command),
        request_timeout=float(timeout),
        env=env,
    )


def _to_mcp_tool(
    server_name: str, tool_def: Any, client: StdioMcpClient
) -> McpTool:
    if not isinstance(tool_def, dict) or not isinstance(tool_def.get("name"), str):
        raise McpProtocolError(
            f"server {server_name!r} listed a tool without a valid name"
        )
    remote_name = tool_def["name"]
    schema = tool_def.get("inputSchema")
    if not isinstance(schema, dict):
        raise McpProtocolError(
            f"server {server_name!r} tool {remote_name!r} has no valid inputSchema"
        )
    registered_name = (
        f"{TOOL_NAME_PREFIX}{server_name}__{_sanitize_name(remote_name)}"
    )
    description = tool_def.get("description") or "(no description)"
    return McpTool(
        name=registered_name,
        description=f"[MCP {server_name}] {description}",
        input_schema=schema,
        client=client,
        server_name=server_name,
        remote_name=remote_name,
    )


def _sanitize_name(name: str) -> str:
    """Map a remote tool name into the registered-name alphabet."""
    sanitized = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return sanitized or "tool"


def _content_to_text(content: Any) -> str:
    """Flatten MCP content blocks into one string for the model."""
    if not isinstance(content, list):
        return str(content)
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text", "")))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(parts)
