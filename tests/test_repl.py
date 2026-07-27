"""Tests for REPL-only commands."""

from types import SimpleNamespace

from sakicode.repl import (
    format_context,
    format_runtime,
    format_toolbar,
    format_traces,
    run_repl,
)
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


def test_format_toolbar_shows_state_and_token_usage():
    agent = SimpleNamespace(
        runtime=AgentStateMachine(),
        context_tokens=1234,
        total_prompt_tokens=2000,
        total_completion_tokens=300,
    )

    toolbar = format_toolbar(agent)

    assert "state: idle" in toolbar
    assert "context: ~1,234 tok" in toolbar
    assert "total: 2,300 tok" in toolbar


def test_format_context_shows_four_layers():
    stats = SimpleNamespace(
        estimated_input_tokens=900,
        max_input_tokens=1000,
        tokenizer="fake:model",
        instruction_tokens=100,
        task_state_tokens=200,
        recent_dialogue_tokens=400,
        tool_result_tokens=200,
        dropped_groups=3,
        trimmed_tool_results=2,
    )

    output = format_context(SimpleNamespace(last_context_stats=stats))

    assert "900/1,000" in output
    assert "instructions=100" in output
    assert "tool_results=200" in output
    assert "compacted_groups=3" in output


def test_repl_recovers_from_undecodable_terminal_input():
    class FakeSession:
        def __init__(self):
            self.inputs = iter(
                [
                    UnicodeDecodeError("utf-8", b"\xe3", 0, 1, "invalid byte"),
                    "/trace",
                    "exit",
                ]
            )

        def prompt(self, _prompt):
            value = next(self.inputs)
            if isinstance(value, BaseException):
                raise value
            return value

    class FakeConsole:
        def __init__(self):
            self.output = []

        def print(self, message):
            self.output.append(message)

    console = FakeConsole()
    agent = SimpleNamespace(
        console=console,
        runtime=AgentStateMachine(),
        tool_registry=SimpleNamespace(traces=[]),
    )

    run_repl(agent, session=FakeSession())

    assert any("please re-enter the command" in line for line in console.output)
    assert "Tool traces: (none)" in console.output
    assert console.output[-1] == "Bye!"


def test_repl_exits_on_slash_exit_without_running_a_turn():
    class FakeSession:
        def __init__(self):
            self.inputs = iter(["/exit"])

        def prompt(self, _prompt):
            return next(self.inputs)

    class FakeConsole:
        def __init__(self):
            self.output = []

        def print(self, message):
            self.output.append(message)

    def run_turn(_text):
        raise AssertionError("run_turn must not be called for /exit")

    console = FakeConsole()
    agent = SimpleNamespace(
        console=console,
        runtime=AgentStateMachine(),
        tool_registry=SimpleNamespace(traces=[]),
        run_turn=run_turn,
    )

    run_repl(agent, session=FakeSession())

    assert console.output[-1] == "Bye!"
