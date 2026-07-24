"""Tests for REPL-only commands."""

from sakicode.repl import format_runtime, format_traces
from sakicode.runtime import AgentState, AgentStateMachine
from sakicode.tooling import FunctionTool, ToolRegistry, ToolResult


def test_format_runtime_before_first_turn():
    runtime = AgentStateMachine()

    assert format_runtime(runtime) == "Runtime state: idle\n(no transitions yet)"


def test_format_runtime_shows_transition_reasons():
    runtime = AgentStateMachine()
    runtime.begin_turn()
    runtime.transition(AgentState.COMPLETED, "model returned final response")

    assert format_runtime(runtime).splitlines() == [
        "Runtime state: completed",
        "1. idle -> requesting_model: user turn started",
        "2. requesting_model -> completed: model returned final response",
    ]


def test_format_traces_shows_outcome_and_redacted_arguments():
    tool = FunctionTool(
        name="login",
        description="test",
        input_schema={
            "type": "object",
            "properties": {"token": {"type": "string"}},
            "required": ["token"],
        },
        handler=lambda token: ToolResult.success("done"),
    )
    registry = ToolRegistry([tool])
    registry.execute("login", {"token": "do-not-print"})

    output = format_traces(registry)

    assert "login [ok]" in output
    assert '"token": "[REDACTED]"' in output
    assert "do-not-print" not in output
