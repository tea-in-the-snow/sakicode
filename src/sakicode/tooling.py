"""Shared tool protocol, structured results, registry, and invocation traces."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
import json
from time import perf_counter
from typing import Any, Protocol, runtime_checkable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class ToolErrorCode(str, Enum):
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    IO_ERROR = "io_error"
    TIMEOUT = "timeout"
    NON_ZERO_EXIT = "non_zero_exit"
    EXECUTION_ERROR = "execution_error"
    PERMISSION_DENIED = "permission_denied"
    TOOL_CALL_LIMIT = "tool_call_limit"


@dataclass(frozen=True)
class ToolResult:
    """A tool outcome kept structured until the model-message boundary."""

    content: str
    is_error: bool = False
    error_code: ToolErrorCode | None = None
    duration_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, content: str, **metadata: Any) -> ToolResult:
        return cls(content=content, metadata=metadata)

    @classmethod
    def error(
        cls, code: ToolErrorCode, content: str, **metadata: Any
    ) -> ToolResult:
        return cls(
            content=content,
            is_error=True,
            error_code=code,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": not self.is_error,
            "content": self.content,
            "error_code": self.error_code.value if self.error_code else None,
            "duration_ms": round(self.duration_ms, 3),
            "metadata": self.metadata,
        }

    def to_model_text(self) -> str:
        """Serialize all result fields so the model can reason about failures."""
        return json.dumps(self.to_dict(), ensure_ascii=False)


@runtime_checkable
class Tool(Protocol):
    name: str
    description: str
    input_schema: Mapping[str, Any]
    requires_confirmation: bool

    def invoke(self, arguments: dict[str, Any]) -> ToolResult:
        """Execute validated arguments and return a structured outcome."""


@dataclass(frozen=True)
class FunctionTool:
    """Adapt a Python callable to the common Tool protocol."""

    name: str
    description: str
    input_schema: Mapping[str, Any]
    handler: Callable[..., ToolResult]
    requires_confirmation: bool = False

    def invoke(self, arguments: dict[str, Any]) -> ToolResult:
        return self.handler(**arguments)

    def openai_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.input_schema),
            },
        }


@dataclass(frozen=True)
class ToolTrace:
    call_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    ok: bool
    error_code: str | None
    duration_ms: float
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolRegistry:
    """Discover, validate, execute, and trace tools through one boundary."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self.traces: list[ToolTrace] = []
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        try:
            Draft202012Validator.check_schema(tool.input_schema)
        except SchemaError as error:
            raise ValueError(f"invalid schema for tool {tool.name!r}: {error.message}") from error
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def requires_confirmation(self, name: str) -> bool:
        tool = self.get(name)
        return bool(tool and tool.requires_confirmation)

    def schemas(self) -> list[dict[str, Any]]:
        schemas = []
        for tool in self._tools.values():
            if isinstance(tool, FunctionTool):
                schemas.append(tool.openai_schema())
            else:
                schemas.append(
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.input_schema),
                        },
                    }
                )
        return schemas

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        call_id: str | None = None,
    ) -> ToolResult:
        validation_error = self.validate(name, arguments)
        if validation_error is not None:
            return self.record_result(
                name, arguments, validation_error, call_id
            )
        tool = self._tools[name]

        started = perf_counter()
        try:
            result = tool.invoke(arguments)
            if not isinstance(result, ToolResult):
                raise TypeError(
                    f"tool {name!r} returned {type(result).__name__}, expected ToolResult"
                )
        except Exception as error:
            result = ToolResult.error(
                ToolErrorCode.EXECUTION_ERROR,
                f"{type(error).__name__}: {error}",
            )
        result = replace(result, duration_ms=(perf_counter() - started) * 1000)
        return self.record_result(name, arguments, result, call_id)

    def validate(
        self, name: str, arguments: dict[str, Any]
    ) -> ToolResult | None:
        """Return a structured validation error without invoking the tool."""
        tool = self.get(name)
        if tool is None:
            return ToolResult.error(
                ToolErrorCode.UNKNOWN_TOOL, f"unknown tool {name!r}"
            )
        errors = sorted(
            Draft202012Validator(tool.input_schema).iter_errors(arguments),
            key=lambda error: list(error.absolute_path),
        )
        if not errors:
            return None
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path)
        prefix = f"{location}: " if location else ""
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"{prefix}{error.message}",
        )

    def record_result(
        self,
        name: str,
        arguments: dict[str, Any],
        result: ToolResult,
        call_id: str | None = None,
    ) -> ToolResult:
        tool = self.get(name)
        schema = tool.input_schema if tool else {}
        self.traces.append(
            ToolTrace(
                call_id=call_id,
                tool_name=name,
                arguments=_redact(arguments, schema),
                ok=not result.is_error,
                error_code=result.error_code.value if result.error_code else None,
                duration_ms=round(result.duration_ms, 3),
                metadata=_redact(dict(result.metadata)),
            )
        )
        return result


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_TRACE_STRING_LIMIT = 200


def _redact(value: Any, schema: Mapping[str, Any] | None = None) -> Any:
    """Redact schema-marked/common secrets and cap large trace-only strings."""
    schema = schema or {}
    if schema.get("x-sensitive") is True:
        return "[REDACTED]"
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        redacted = {}
        for key, item in value.items():
            child_schema = properties.get(key, {})
            if _is_sensitive_key(key):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item, child_schema)
        return redacted
    if isinstance(value, list):
        item_schema = schema.get("items", {})
        return [_redact(item, item_schema) for item in value]
    if isinstance(value, str) and len(value) > _TRACE_STRING_LIMIT:
        return value[:_TRACE_STRING_LIMIT] + "... [trace truncated]"
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SENSITIVE_KEYS:
        return True
    return normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )
