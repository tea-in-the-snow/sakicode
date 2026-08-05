"""M5 acceptance tests: durable, migratable, secret-safe checkpoints."""

import json

import pytest
from rich.console import Console

from sakicode.agent import Agent
from sakicode.checkpoint import (
    CheckpointCorruptError,
    CheckpointError,
    CheckpointNotFoundError,
    CheckpointStore,
    SensitiveCheckpointError,
    UnsupportedCheckpointVersion,
    WorkspaceMismatchError,
)
from sakicode.permissions import PermissionEngine
from sakicode.runtime import AgentState
from sakicode.tooling import ToolErrorCode, ToolResult


def _completed_agent(workspace, store, session_id="session-1"):
    agent = Agent(
        client=object(),
        model="test-model",
        system_prompt="system",
        console=Console(quiet=True),
        permission_engine=PermissionEngine(workspace),
        checkpoint_store=store,
        session_id=session_id,
    )
    agent.messages.extend(
        [
            {"role": "user", "content": "inspect README"},
            {"role": "assistant", "content": "done"},
        ]
    )
    agent.task_summary = "FACTS:\n- README inspected"
    agent.last_prompt_tokens = 120
    agent.total_prompt_tokens = 220
    agent.total_completion_tokens = 30
    agent.runtime.begin_turn()
    agent.runtime.transition(AgentState.COMPLETED, "model returned final response")

    policy = agent.permission_engine.evaluate(
        "write_file", {"path": "notes.txt", "content": "x"}
    )
    agent.permission_engine.approve_session(policy)
    agent.permission_engine.record("write_file", policy, "allow_session")
    result = ToolResult.error(ToolErrorCode.IO_ERROR, "missing")
    agent.tool_registry.record_result(
        "read_file", {"path": "missing.txt"}, result, "call-1"
    )
    return agent


def test_session_round_trip_restores_all_long_lived_state(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    original = _completed_agent(tmp_path, store)

    path = original.save_checkpoint()
    restored = store.load("session-1")
    resumed = Agent(
        client=object(),
        model="test-model",
        system_prompt="new process prompt",
        console=Console(quiet=True),
        permission_engine=PermissionEngine(tmp_path),
        checkpoint_store=store,
        session_id="session-1",
    )
    resumed.restore_checkpoint(restored)

    assert path.name == "session-1.json"
    assert resumed.messages == original.messages
    assert resumed.task_summary == original.task_summary
    assert resumed.runtime.state is AgentState.COMPLETED
    assert resumed.total_prompt_tokens == 220
    assert resumed.total_completion_tokens == 30
    assert resumed.permission_engine.session_grants == ["workspace-write"]
    assert resumed.permission_engine.audit_log[0].outcome == "allow_session"
    assert resumed.tool_registry.traces[0].call_id == "call-1"

    # A terminal checkpoint can begin a new turn after a process restart.
    resumed._stream_response = lambda: ("continued", [])
    resumed.run_turn("continue")
    assert resumed.runtime.state is AgentState.COMPLETED
    assert store.load("session-1").payload["agent"]["messages"][-1]["content"] == "continued"


def test_atomic_replace_failure_preserves_previous_checkpoint(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    agent = _completed_agent(tmp_path, store)
    path = agent.save_checkpoint()
    before = path.read_bytes()
    agent.messages.append({"role": "user", "content": "new state"})

    def fail_replace(_source, _target):
        raise OSError("simulated crash before replace")

    monkeypatch.setattr("sakicode.checkpoint.os.replace", fail_replace)
    with pytest.raises(CheckpointError, match="atomically save"):
        agent.save_checkpoint()

    assert path.read_bytes() == before
    assert list(path.parent.glob("*.tmp")) == []
    assert store.load("session-1").payload["agent"]["messages"][-1]["content"] == "done"


def test_half_written_temp_file_is_not_a_checkpoint(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    store.directory.mkdir()
    (store.directory / ".half-write.tmp").write_text('{"schema_version": 2')

    with pytest.raises(CheckpointNotFoundError):
        store.load("half-write")


def test_v1_checkpoint_is_migrated_in_memory(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    agent = _completed_agent(tmp_path, store)
    current = json.loads(agent.save_checkpoint().read_text())
    old = {
        "schema_version": 1,
        "session_id": current["session_id"],
        "saved_at": current["saved_at"],
        "workspace_root": current["workspace"]["root"],
        "model": current["agent"]["model"],
        "messages": current["agent"]["messages"],
        "task_summary": current["agent"]["task_summary"],
        "runtime": current["agent"]["runtime"],
        "budget_usage": current["agent"]["budget"],
        "session_grants": current["permissions"]["session_grants"],
        "approval_audit": current["permissions"]["audit_log"],
        "tool_traces": current["tool_traces"],
    }
    store.path_for("session-1").write_text(json.dumps(old))

    restored = store.load("session-1")

    assert restored.migrated_from == 1
    assert restored.payload["schema_version"] == 2
    # Loading never rewrites the source; migration becomes durable on next save.
    assert json.loads(store.path_for("session-1").read_text())["schema_version"] == 1


def test_unknown_schema_and_corrupt_data_are_rejected(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    agent = _completed_agent(tmp_path, store)
    path = agent.save_checkpoint()
    payload = json.loads(path.read_text())
    payload["schema_version"] = 99
    path.write_text(json.dumps(payload))
    with pytest.raises(UnsupportedCheckpointVersion):
        store.load("session-1")

    payload["schema_version"] = 2
    payload["agent"]["runtime"]["state"] = "executing_tools"
    path.write_text(json.dumps(payload))
    with pytest.raises(CheckpointCorruptError, match="runtime.state"):
        store.load("session-1")


def test_semantically_impossible_runtime_history_is_rejected(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    path = _completed_agent(tmp_path, store).save_checkpoint()
    payload = json.loads(path.read_text())
    payload["agent"]["runtime"]["history"][0]["previous"] = "completed"
    path.write_text(json.dumps(payload))

    with pytest.raises(CheckpointCorruptError, match="runtime/message invariants"):
        store.load("session-1")


def test_secrets_are_redacted_on_save_and_rejected_on_load(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    agent = _completed_agent(tmp_path, store)
    agent.messages[1]["content"] = "OPENAI_API_KEY=sk-abcdefghijk"
    path = agent.save_checkpoint()

    text = path.read_text()
    assert "sk-abcdefghijk" not in text
    assert "[REDACTED]" in text

    payload = json.loads(text)
    payload["tool_traces"][0]["metadata"]["api_key"] = "sk-leakedvalue"
    path.write_text(json.dumps(payload))
    with pytest.raises(SensitiveCheckpointError):
        store.load("session-1")


def test_checkpoint_is_bound_to_the_workspace_identity(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    directory = tmp_path / "shared-checkpoints"
    store = CheckpointStore(first, directory)
    _completed_agent(first, store).save_checkpoint()

    with pytest.raises(WorkspaceMismatchError):
        CheckpointStore(second, directory).load("session-1")


def test_interrupted_half_tool_bundle_is_trimmed_to_a_valid_prefix(tmp_path):
    store = CheckpointStore(tmp_path, tmp_path / "checkpoints")
    agent = _completed_agent(tmp_path, store)
    agent.runtime = agent.runtime.__class__()
    agent.runtime.begin_turn()
    agent.messages.extend(
        [
            {"role": "user", "content": "read two files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "a",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "b",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "first"},
        ]
    )
    agent.runtime.transition(AgentState.INTERRUPTED, "process interrupted")

    agent.save_checkpoint()
    messages = store.load("session-1").payload["agent"]["messages"]

    assert messages[-1] == {"role": "user", "content": "read two files"}
    assert all(message.get("tool_call_id") != "a" for message in messages)
