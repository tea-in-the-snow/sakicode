"""The agent: conversation state, streaming responses, and the tool-use loop."""

import json
import sys

from openai import APIError
from rich.console import Console

from . import tools
from .runtime import AgentState, AgentStateMachine
from .tooling import ToolErrorCode, ToolRegistry, ToolResult

MAX_TOOL_CALLS_PER_TURN = 25
CONTEXT_WINDOW_TOKENS = 128_000


class Agent:
    def __init__(
        self,
        client,
        model: str,
        system_prompt: str,
        console: Console | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self.client = client
        self.model = model
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.console = console or Console()
        self._context_warning_shown = False
        self.runtime = AgentStateMachine()
        self.tool_registry = tool_registry or tools.create_registry()
        self.last_prompt_tokens: int | None = None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    @property
    def context_tokens(self) -> int:
        """Best-known context size: API-reported prompt tokens, else a rough estimate."""
        if self.last_prompt_tokens is not None:
            return self.last_prompt_tokens
        return sum(len(str(m.get("content") or "")) for m in self.messages) // 4

    def run_turn(self, user_text: str) -> None:
        """Handle one user message: stream replies and run tools until done."""
        self.messages.append({"role": "user", "content": user_text})
        self._check_context_size()
        tool_calls_made = 0
        self.runtime.begin_turn()
        try:
            while True:
                content, tool_calls = self._stream_response()
                assistant_message: dict = {"role": "assistant", "content": content or None}
                if not tool_calls:
                    self.messages.append(assistant_message)
                    self.runtime.transition(AgentState.COMPLETED, "model returned final response")
                    return
                assistant_message["tool_calls"] = tool_calls
                self.messages.append(assistant_message)
                self.runtime.transition(AgentState.EXECUTING_TOOLS, "model requested tools")
                limit_hit = False
                for tool_call in tool_calls:
                    if tool_calls_made >= MAX_TOOL_CALLS_PER_TURN:
                        result = self._tool_limit_result(tool_call)
                        limit_hit = True
                    else:
                        result = self._execute_tool(tool_call)
                        tool_calls_made += 1
                    self.messages.append(
                        {"role": "tool", "tool_call_id": tool_call["id"], "content": result}
                    )
                if limit_hit or tool_calls_made >= MAX_TOOL_CALLS_PER_TURN:
                    self.console.print(
                        "[yellow]Stopped after "
                        f"{MAX_TOOL_CALLS_PER_TURN} tool calls in one turn.[/yellow]"
                    )
                    self.runtime.transition(AgentState.LIMIT_REACHED, "tool call budget exhausted")
                    return
                self.runtime.transition(AgentState.REQUESTING_MODEL, "tool results ready")
        except APIError as e:
            self.runtime.transition(AgentState.FAILED, f"model API error: {type(e).__name__}")
            self.console.print(f"[red]API error: {e}[/red]")
        except KeyboardInterrupt:
            self.runtime.transition(AgentState.INTERRUPTED, "user interrupted turn")
            raise

    def _stream_response(self) -> tuple[str, list[dict]]:
        """Stream one completion, printing text as it arrives.

        Tool-call deltas arrive fragmented across chunks and are merged by
        index: ids/names/argument strings are concatenated.
        """
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tool_registry.schemas(),
            stream=True,
            stream_options={"include_usage": True},
        )
        content_parts: list[str] = []
        pending: dict[int, dict] = {}
        for chunk in stream:
            if chunk.usage:
                # The final usage chunk reports the whole request's totals.
                self.last_prompt_tokens = chunk.usage.prompt_tokens
                self.total_prompt_tokens += chunk.usage.prompt_tokens
                self.total_completion_tokens += chunk.usage.completion_tokens
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
            result = ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"arguments are not valid JSON: {e}",
            )
            self.tool_registry.record_result(name, {}, result, tool_call.get("id"))
            return result.to_model_text()
        if not isinstance(args, dict):
            result = ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENTS,
                "tool arguments must be a JSON object",
            )
            self.tool_registry.record_result(name, {}, result, tool_call.get("id"))
            return result.to_model_text()
        validation_error = self.tool_registry.validate(name, args)
        if validation_error is not None:
            self.tool_registry.record_result(
                name, args, validation_error, tool_call.get("id")
            )
            return validation_error.to_model_text()
        if self.tool_registry.requires_confirmation(name):
            self.runtime.transition(AgentState.WAITING_APPROVAL, f"approval required for {name}")
            approved = self._confirm(name, args)
            self.runtime.transition(
                AgentState.EXECUTING_TOOLS,
                f"approval {'granted' if approved else 'denied'} for {name}",
            )
            if not approved:
                result = ToolResult.error(
                    ToolErrorCode.PERMISSION_DENIED,
                    "permission denied by user",
                )
                self.tool_registry.record_result(
                    name, args, result, tool_call.get("id")
                )
                return result.to_model_text()
        self.console.print(f"[dim]→ {name}({_summarize(name, args)})[/dim]")
        result = self.tool_registry.execute(name, args, tool_call.get("id"))
        outcome = "error" if result.is_error else "ok"
        self.console.print(
            f"[dim]← {name}: {outcome} ({result.duration_ms:.1f} ms)[/dim]"
        )
        return result.to_model_text()

    def _tool_limit_result(self, tool_call: dict) -> str:
        """Close a skipped tool call with a structured, traced result."""
        name = tool_call["function"]["name"]
        try:
            arguments = json.loads(tool_call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        result = ToolResult.error(
            ToolErrorCode.TOOL_CALL_LIMIT,
            "per-turn tool call limit reached; call skipped",
            limit=MAX_TOOL_CALLS_PER_TURN,
        )
        self.tool_registry.record_result(
            name, arguments, result, tool_call.get("id")
        )
        return result.to_model_text()

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
                "[yellow]Warning: context is getting large "
                f"(~{estimated_tokens:,} tokens).[/yellow]"
            )
            self._context_warning_shown = True


def _summarize(name: str, args: dict) -> str:
    """One-line summary of a tool call: the path, or the command."""
    text = args.get("command") if name == "run_bash" else args.get("path", "")
    text = str(text).replace("\n", " ")
    return text if len(text) <= 80 else text[:77] + "..."
