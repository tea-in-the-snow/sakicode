"""Tests for the shared tool protocol and registry."""

from dataclasses import dataclass

import pytest

from sakicode.tooling import (
    FunctionTool,
    ToolErrorCode,
    ToolRegistry,
    ToolResult,
)


def make_echo_tool(handler=None):
    return FunctionTool(
        name="echo",
        description="Return a message.",
        input_schema={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "api_key": {"type": "string"},
            },
            "required": ["message"],
            "additionalProperties": False,
        },
        handler=handler or (lambda message, api_key=None: ToolResult.success(message)),
    )


def test_registry_discovers_tools_as_openai_schemas():
    registry = ToolRegistry([make_echo_tool()])

    assert registry.schemas() == [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Return a message.",
                "parameters": make_echo_tool().input_schema,
            },
        }
    ]


def test_registry_rejects_duplicate_name_and_invalid_schema():
    registry = ToolRegistry([make_echo_tool()])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_echo_tool())

    invalid = make_echo_tool()
    object.__setattr__(invalid, "input_schema", {"type": "not-a-json-type"})
    with pytest.raises(ValueError, match="invalid schema"):
        ToolRegistry([invalid])


def test_registry_validates_arguments_before_invocation():
    invoked = False

    def handler(message):
        nonlocal invoked
        invoked = True
        return ToolResult.success(message)

    registry = ToolRegistry([make_echo_tool(handler)])
    result = registry.execute("echo", {"message": 42})

    assert result.is_error
    assert result.error_code is ToolErrorCode.INVALID_ARGUMENTS
    assert invoked is False


def test_validate_does_not_invoke_or_record_trace():
    invoked = False

    def handler(message):
        nonlocal invoked
        invoked = True
        return ToolResult.success(message)

    registry = ToolRegistry([make_echo_tool(handler)])

    validation_error = registry.validate("echo", {"message": 42})

    assert validation_error.error_code is ToolErrorCode.INVALID_ARGUMENTS
    assert invoked is False
    assert registry.traces == []


def test_registry_categorizes_unknown_tool_and_handler_exception():
    registry = ToolRegistry(
        [make_echo_tool(lambda message, api_key=None: 1 / 0)]
    )

    unknown = registry.execute("missing", {})
    crashed = registry.execute("echo", {"message": "hello"})

    assert unknown.error_code is ToolErrorCode.UNKNOWN_TOOL
    assert crashed.error_code is ToolErrorCode.EXECUTION_ERROR
    assert "ZeroDivisionError" in crashed.content


def test_result_contains_duration_and_trace_redacts_secret():
    registry = ToolRegistry([make_echo_tool()])

    result = registry.execute(
        "echo",
        {"message": "hello", "api_key": "sk-super-secret"},
        call_id="call-7",
    )

    assert result.duration_ms >= 0
    assert result.to_dict()["ok"] is True
    trace = registry.traces[-1]
    assert trace.call_id == "call-7"
    assert trace.arguments == {
        "message": "hello",
        "api_key": "[REDACTED]",
    }
    assert "sk-super-secret" not in str(trace.to_dict())


def test_trace_recursively_redacts_common_secret_names_in_metadata():
    tool = make_echo_tool(
        lambda message, api_key=None: ToolResult.success(
            message,
            auth={"access_token": "nested-secret"},
        )
    )
    registry = ToolRegistry([tool])

    registry.execute("echo", {"message": "hello"})

    assert registry.traces[-1].metadata == {
        "auth": {"access_token": "[REDACTED]"}
    }


def test_schema_marked_field_is_redacted():
    tool = FunctionTool(
        name="write",
        description="test",
        input_schema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "x-sensitive": True}
            },
            "required": ["content"],
        },
        handler=lambda content: ToolResult.success("written"),
    )
    registry = ToolRegistry([tool])

    registry.execute("write", {"content": "private source code"})

    assert registry.traces[-1].arguments["content"] == "[REDACTED]"


@dataclass
class BadTool:
    name: str = "bad"
    description: str = "Returns the wrong result type."
    input_schema: dict = None
    requires_confirmation: bool = False

    def __post_init__(self):
        self.input_schema = {"type": "object"}

    def invoke(self, arguments):
        return "not structured"


def test_registry_rejects_unstructured_handler_result():
    result = ToolRegistry([BadTool()]).execute("bad", {})

    assert result.error_code is ToolErrorCode.EXECUTION_ERROR
    assert "expected ToolResult" in result.content
