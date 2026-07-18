"""The agent: conversation state, streaming responses, and the tool-use loop."""

import json
import sys

from openai import APIError
from rich.console import Console

from . import tools

MAX_TOOL_CALLS_PER_TURN = 25
CONTEXT_WINDOW_TOKENS = 128_000


class Agent:
    def __init__(self, client, model: str, system_prompt: str, console: Console | None = None):
        self.client = client
        self.model = model
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.console = console or Console()
        self._context_warning_shown = False

    def run_turn(self, user_text: str) -> None:
        """Handle one user message: stream replies and run tools until done."""
        self.messages.append({"role": "user", "content": user_text})
        self._check_context_size()
        tool_calls_made = 0
        while True:
            try:
                content, tool_calls = self._stream_response()
            except APIError as e:
                self.console.print(f"[red]API error: {e}[/red]")
                return
            assistant_message: dict = {"role": "assistant", "content": content or None}
            if not tool_calls:
                self.messages.append(assistant_message)
                return
            assistant_message["tool_calls"] = tool_calls
            self.messages.append(assistant_message)
            limit_hit = False
            for tool_call in tool_calls:
                if tool_calls_made >= MAX_TOOL_CALLS_PER_TURN:
                    result = "Error: per-turn tool call limit reached; call skipped."
                    limit_hit = True
                else:
                    result = self._execute_tool(tool_call)
                    tool_calls_made += 1
                self.messages.append(
                    {"role": "tool", "tool_call_id": tool_call["id"], "content": result}
                )
            if limit_hit or tool_calls_made >= MAX_TOOL_CALLS_PER_TURN:
                self.console.print(
                    f"[yellow]Stopped after {MAX_TOOL_CALLS_PER_TURN} tool calls in one turn.[/yellow]"
                )
                return

    def _stream_response(self) -> tuple[str, list[dict]]:
        """Stream one completion, printing text as it arrives.

        Tool-call deltas arrive fragmented across chunks and are merged by
        index: ids/names/argument strings are concatenated.
        """
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=tools.TOOL_SCHEMAS,
            stream=True,
        )
        content_parts: list[str] = []
        pending: dict[int, dict] = {}
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                content_parts.append(delta.content)
                self.console.print(delta.content, end="", markup=False, highlight=False)
            for tc_delta in delta.tool_calls or []:
                slot = pending.setdefault(
                    tc_delta.index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if tc_delta.id:
                    slot["id"] += tc_delta.id
                if tc_delta.function:
                    if tc_delta.function.name:
                        slot["function"]["name"] += tc_delta.function.name
                    if tc_delta.function.arguments:
                        slot["function"]["arguments"] += tc_delta.function.arguments
        if content_parts:
            self.console.print()  # end the streamed line
        return "".join(content_parts), [pending[i] for i in sorted(pending)]

    def _execute_tool(self, tool_call: dict) -> str:
        """Run one tool call (with permission gate); errors become tool results."""
        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"]["arguments"] or "{}")
        except json.JSONDecodeError as e:
            return f"Error: invalid tool arguments: {e}"
        if name in tools.REQUIRES_CONFIRMATION and not self._confirm(name, args):
            return "Error: permission denied by user"
        self.console.print(f"[dim]→ {name}({_summarize(name, args)})[/dim]")
        try:
            return tools.dispatch(name, args)
        except Exception as e:  # never crash the turn on tool errors
            return f"Error: {type(e).__name__}: {e}"

    def _confirm(self, name: str, args: dict) -> bool:
        """Ask the user before running a mutating tool. Default is deny."""
        self.console.print(f"[yellow]Tool {name!r} wants to run:[/yellow] {_summarize(name, args)}")
        if not sys.stdin.isatty():
            self.console.print("[yellow]stdin is not a TTY; denying automatically.[/yellow]")
            return False
        try:
            answer = input("Allow? [y/N] ")
        except EOFError:
            return False
        return answer.strip().lower() in ("y", "yes")

    def _check_context_size(self) -> None:
        """Warn once when the rough token estimate exceeds 80% of the window."""
        if self._context_warning_shown:
            return
        total_chars = sum(len(str(m.get("content") or "")) for m in self.messages)
        estimated_tokens = total_chars // 4
        if estimated_tokens > 0.8 * CONTEXT_WINDOW_TOKENS:
            self.console.print(
                f"[yellow]Warning: context is getting large (~{estimated_tokens:,} tokens).[/yellow]"
            )
            self._context_warning_shown = True


def _summarize(name: str, args: dict) -> str:
    """One-line summary of a tool call: the path, or the command."""
    text = args.get("command") if name == "run_bash" else args.get("path", "")
    text = str(text).replace("\n", " ")
    return text if len(text) <= 80 else text[:77] + "..."
