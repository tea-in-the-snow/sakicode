"""M4 acceptance tests for layered context and token budgets."""

import json
import sys
from types import SimpleNamespace

import pytest

from sakicode.context import (
    ContextBudget,
    ContextManager,
    InvalidMessageHistory,
    TokenCounter,
)


def _budget(**overrides):
    values = {
        "max_input_tokens": 1_600,
        "max_output_tokens": 400,
        "instruction_tokens": 250,
        "task_state_tokens": 350,
        "recent_dialogue_tokens": 700,
        "tool_result_tokens": 250,
        "max_tool_result_tokens": 180,
    }
    values.update(overrides)
    return ContextBudget(**values)


def _call(call_id="call-1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
    }


def test_model_tokenizer_is_used_when_supplied():
    counter = TokenCounter("exact-model", encode=lambda text: text.split(), tokenizer_name="fake")

    assert counter.count_text("one two three") == 3
    assert counter.is_model_specific is True
    assert counter.tokenizer_name == "fake"


def test_known_openai_model_resolves_model_encoding(monkeypatch):
    encoding = SimpleNamespace(name="model-encoding", encode=lambda text: text.split())
    fake_tiktoken = SimpleNamespace(encoding_for_model=lambda model: encoding)
    monkeypatch.setitem(sys.modules, "tiktoken", fake_tiktoken)

    counter = TokenCounter.for_model("gpt-4o-mini")

    assert counter.is_model_specific is True
    assert counter.tokenizer_name == "tiktoken:model-encoding"
    assert counter.count_text("one two three") == 3


def test_unknown_model_uses_conservative_utf8_estimate():
    counter = TokenCounter.for_model("unknown-provider/model")

    assert counter.is_model_specific is False
    assert counter.count_text("汉字") == len("汉字".encode("utf-8"))
    assert counter.count_text("punctuation!!!!") == len("punctuation!!!!")


def test_tokenizer_load_failure_falls_back_safely(monkeypatch):
    def unavailable(_model):
        raise OSError("offline")

    monkeypatch.setitem(
        sys.modules, "tiktoken", SimpleNamespace(encoding_for_model=unavailable)
    )

    counter = TokenCounter.for_model("gpt-offline")

    assert counter.is_model_specific is False
    assert counter.tokenizer_name == "conservative:utf8-bytes"


def test_large_structured_tool_result_is_trimmed_without_breaking_pair():
    manager = ContextManager(
        "unknown",
        _budget(max_input_tokens=1_700, tool_result_tokens=350, max_tool_result_tokens=220),
    )
    payload = {
        "ok": True,
        "content": "HEAD-" + "x" * 500 + "-TAIL",
        "error_code": None,
        "duration_ms": 1,
        "metadata": {},
    }
    messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": None, "tool_calls": [_call()]},
        {"role": "tool", "tool_call_id": "call-1", "content": json.dumps(payload)},
    ]

    prepared = manager.prepare(messages)

    assert [message["role"] for message in prepared.messages[-2:]] == ["assistant", "tool"]
    trimmed = json.loads(prepared.messages[-1]["content"])
    assert trimmed["metadata"]["context_truncated"] is True
    assert "HEAD-" in trimmed["content"]
    assert "-TAIL" in trimmed["content"]
    assert prepared.stats.trimmed_tool_results == 1
    # Request compaction is a view; the lossless Agent history is untouched.
    assert len(json.loads(messages[-1]["content"])["content"]) == 510
    ContextManager.validate_tool_pairs(prepared.messages)


def test_long_history_is_bounded_summarized_and_keeps_key_fact():
    manager = ContextManager("unknown", _budget())
    messages = [
        {"role": "system", "content": "Never lose the current user request."},
        {"role": "user", "content": "FACT: deployment region is ap-southeast-1."},
        {"role": "assistant", "content": "DECISION: use SQLite for the prototype."},
    ]
    for index in range(18):
        messages.extend(
            [
                {"role": "user", "content": f"old question {index} " + "q" * 40},
                {"role": "assistant", "content": f"old answer {index} " + "a" * 40},
            ]
        )
    messages.append({"role": "user", "content": "CURRENT: finish the budget tests."})

    prepared = manager.prepare(messages)

    assert prepared.stats.estimated_input_tokens <= prepared.stats.max_input_tokens
    assert prepared.stats.dropped_groups > 0
    assert prepared.stats.task_state_tokens <= manager.budget.task_state_tokens
    assert "deployment region is ap-southeast-1" in prepared.task_summary
    assert "untrusted historical DATA" in prepared.task_summary
    assert prepared.messages[-1]["content"] == "CURRENT: finish the budget tests."


def test_compaction_never_splits_a_tool_call_bundle():
    manager = ContextManager("unknown", _budget())
    messages = [{"role": "system", "content": "system"}]
    for index in range(8):
        call_id = f"call-{index}"
        messages.extend(
            [
                {"role": "assistant", "content": None, "tool_calls": [_call(call_id)]},
                {"role": "tool", "tool_call_id": call_id, "content": "result " + "x" * 80},
            ]
        )
    messages.append({"role": "user", "content": "what did we learn?"})

    prepared = manager.prepare(messages)

    ContextManager.validate_tool_pairs(prepared.messages)
    assistant_ids = {
        call["id"]
        for message in prepared.messages
        for call in message.get("tool_calls", [])
    }
    result_ids = {
        message["tool_call_id"]
        for message in prepared.messages
        if message.get("role") == "tool"
    }
    assert assistant_ids == result_ids


@pytest.mark.parametrize(
    "history, error",
    [
        ([{"role": "tool", "tool_call_id": "x", "content": "orphan"}], "orphan"),
        ([{"role": "assistant", "content": None, "tool_calls": [_call("x")]}], "missing"),
        (
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [_call("x"), _call("x")],
                },
                {"role": "tool", "tool_call_id": "x", "content": "one"},
                {"role": "tool", "tool_call_id": "x", "content": "two"},
            ],
            "unique",
        ),
    ],
)
def test_invalid_tool_pairing_is_rejected(history, error):
    manager = ContextManager("unknown", _budget())

    with pytest.raises(InvalidMessageHistory, match=error):
        manager.prepare([{"role": "system", "content": "system"}, *history])


def test_layer_configuration_cannot_overbook_input_budget():
    with pytest.raises(ValueError, match="layer budgets"):
        ContextBudget(
            max_input_tokens=100,
            instruction_tokens=30,
            task_state_tokens=30,
            recent_dialogue_tokens=30,
            tool_result_tokens=30,
        )
