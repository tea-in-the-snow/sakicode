"""Tests for token-usage tracking in the agent's streaming loop."""

import io
from types import SimpleNamespace

from rich.console import Console

from sakicode.agent import Agent
from sakicode.tooling import ToolRegistry


def _content_chunk(text):
    delta = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def _usage_chunk(prompt_tokens, completion_tokens):
    return SimpleNamespace(
        choices=[],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        ),
    )


def _agent(chunks):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return iter(chunks)

    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    agent = Agent(
        client=client,
        model="test-model",
        system_prompt="s",
        console=Console(file=io.StringIO()),
        tool_registry=ToolRegistry([]),
    )
    return agent, calls


def test_stream_response_accumulates_token_usage():
    agent, calls = _agent([_content_chunk("hi"), _usage_chunk(120, 30)])

    content, tool_calls = agent._stream_response()

    assert content == "hi"
    assert tool_calls == []
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert agent.last_prompt_tokens == 120
    assert agent.total_prompt_tokens == 120
    assert agent.total_completion_tokens == 30
    assert agent.context_tokens == 120


def test_stream_response_sums_usage_across_requests():
    agent, calls = _agent([_usage_chunk(100, 10)])

    # FakeCompletions replays the same chunks, so two calls = two requests.
    agent._stream_response()
    agent._stream_response()

    assert len(calls) == 2
    assert agent.total_prompt_tokens == 200
    assert agent.total_completion_tokens == 20
    assert agent.last_prompt_tokens == 100


def test_context_tokens_falls_back_to_character_estimate():
    agent, _ = _agent([])
    agent.messages.append({"role": "user", "content": "x" * 40})

    # System prompt "s" (1 char) + 40 chars = 41 chars -> 10 tokens.
    assert agent.context_tokens == 41 // 4
