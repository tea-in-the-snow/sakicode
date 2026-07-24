"""Tests for the explicit agent runtime state machine."""

import json

import pytest
from rich.console import Console

from sakicode.agent import Agent
from sakicode.runtime import (
    AgentState,
    AgentStateMachine,
    InvalidStateTransition,
)


def test_state_machine_records_serializable_transitions():
    runtime = AgentStateMachine()

    runtime.begin_turn()
    runtime.transition(AgentState.EXECUTING_TOOLS, "model requested tools")
    runtime.transition(AgentState.REQUESTING_MODEL, "tool results ready")
    runtime.transition(AgentState.COMPLETED, "model returned final response")

    assert runtime.state is AgentState.COMPLETED
    assert runtime.snapshot()["history"][-1] == {
        "previous": "requesting_model",
        "current": "completed",
        "reason": "model returned final response",
    }


def test_state_machine_rejects_invalid_transition():
    runtime = AgentStateMachine()

    with pytest.raises(InvalidStateTransition, match="idle.*completed"):
        runtime.transition(AgentState.COMPLETED, "cannot finish before starting")


class ScriptedAgent(Agent):
    def __init__(self, responses):
        super().__init__(
            client=object(),
            model="test-model",
            system_prompt="test",
            console=Console(quiet=True),
        )
        self._responses = iter(responses)

    def _stream_response(self):
        return next(self._responses)

    def _execute_tool(self, tool_call):
        return "tool result"


def test_agent_loop_moves_through_model_tool_and_completion_states():
    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
    }
    agent = ScriptedAgent(
        [
            ("I will inspect it.", [tool_call]),
            ("The task is complete.", []),
        ]
    )

    agent.run_turn("inspect the project")

    assert agent.runtime.state is AgentState.COMPLETED
    assert [event.current for event in agent.runtime.history] == [
        AgentState.REQUESTING_MODEL,
        AgentState.EXECUTING_TOOLS,
        AgentState.REQUESTING_MODEL,
        AgentState.COMPLETED,
    ]
    assert agent.messages[-2] == {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "tool result",
    }


class DeniedApprovalAgent(ScriptedAgent):
    _execute_tool = Agent._execute_tool

    def _confirm(self, name, args):
        return False


def test_denied_approval_is_recorded_and_returned_to_model(tmp_path):
    target = tmp_path / "should-not-exist.txt"
    tool_call = {
        "id": "call-2",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": str(target), "content": "no"}),
        },
    }
    agent = DeniedApprovalAgent(
        [
            ("I need approval.", [tool_call]),
            ("The write was denied.", []),
        ]
    )

    agent.run_turn("write a file")

    assert not target.exists()
    assert AgentState.WAITING_APPROVAL in [
        event.current for event in agent.runtime.history
    ]
    result = json.loads(agent.messages[-2]["content"])
    assert result["ok"] is False
    assert result["error_code"] == "permission_denied"
    assert result["content"] == "permission denied by user"


class ActualToolAgent(ScriptedAgent):
    _execute_tool = Agent._execute_tool


class ConfirmationMustNotRunAgent(ActualToolAgent):
    def _confirm(self, name, args):
        raise AssertionError("invalid arguments must be rejected before approval")


def test_schema_validation_happens_before_approval():
    tool_call = {
        "id": "call-invalid",
        "type": "function",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": 42, "content": "no"}),
        },
    }
    agent = ConfirmationMustNotRunAgent(
        [
            ("I will write.", [tool_call]),
            ("The arguments were invalid.", []),
        ]
    )

    agent.run_turn("write a file")

    result = json.loads(agent.messages[-2]["content"])
    assert result["error_code"] == "invalid_arguments"


def test_skipped_call_at_tool_limit_has_structured_trace(tmp_path, monkeypatch):
    monkeypatch.setattr("sakicode.agent.MAX_TOOL_CALLS_PER_TURN", 1)
    target = tmp_path / "input.txt"
    target.write_text("hello")
    tool_calls = [
        {
            "id": f"call-{index}",
            "type": "function",
            "function": {
                "name": "read_file",
                "arguments": json.dumps({"path": str(target)}),
            },
        }
        for index in (1, 2)
    ]
    agent = ActualToolAgent([("I will read twice.", tool_calls)])

    agent.run_turn("read twice")

    skipped = json.loads(agent.messages[-1]["content"])
    assert skipped["error_code"] == "tool_call_limit"
    assert agent.tool_registry.traces[-1].call_id == "call-2"
    assert agent.tool_registry.traces[-1].error_code == "tool_call_limit"
