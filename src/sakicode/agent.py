"""The agent: conversation state, streaming responses, and the tool-use loop."""

from collections.abc import Callable
import json
import sys
from pathlib import Path

from openai import APIError
from rich.console import Console

from . import tools
from .checkpoint import CheckpointError, CheckpointStore, RestoredCheckpoint
from .context import ContextBudget, ContextBudgetError, ContextManager, ContextStats
from .permissions import Decision, PermissionEngine, PolicyDecision
from .runtime import AgentState, AgentStateMachine
from .tooling import ToolErrorCode, ToolRegistry, ToolResult

MAX_TOOL_CALLS_PER_TURN = 25


class Agent:
    def __init__(
        self,
        client,
        model: str,
        system_prompt: str,
        console: Console | None = None,
        tool_registry: ToolRegistry | None = None,
        permission_engine: PermissionEngine | None = None,
        context_manager: ContextManager | None = None,
        context_budget: ContextBudget | None = None,
        checkpoint_store: CheckpointStore | None = None,
        session_id: str | None = None,
        approval_handler: Callable[[PolicyDecision], str] | None = None,
    ):
        self.client = client
        self.model = model
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.console = console or Console()
        self.runtime = AgentStateMachine()
        self.tool_registry = tool_registry or tools.create_registry()
        self.permission_engine = permission_engine or PermissionEngine(Path.cwd())
        self.context_manager = context_manager or ContextManager(
            model, budget=context_budget
        )
        self.last_context_stats = None
        self.task_summary: str | None = None
        self.last_prompt_tokens: int | None = None
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.checkpoint_store = checkpoint_store
        self.approval_handler = approval_handler
        self.session_id = session_id or (
            checkpoint_store.new_session_id() if checkpoint_store is not None else None
        )

    @property
    def context_tokens(self) -> int:
        """Best-known size of the most recent model request."""
        if self.last_prompt_tokens is not None:
            return self.last_prompt_tokens
        if self.last_context_stats is not None:
            return self.last_context_stats.estimated_input_tokens
        return self.context_manager.estimate_messages(self.messages)

    def run_turn(self, user_text: str) -> None:
        """Handle one user message: stream replies and run tools until done."""
        self.messages.append({"role": "user", "content": user_text})
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
        except ContextBudgetError as e:
            self.runtime.transition(AgentState.FAILED, "mandatory context exceeds token budget")
            self.console.print(f"[red]Context budget error: {e}[/red]")
        except KeyboardInterrupt:
            self.runtime.transition(AgentState.INTERRUPTED, "user interrupted turn")
            raise
        finally:
            if self.checkpoint_store is not None and self.runtime.state in {
                AgentState.COMPLETED,
                AgentState.FAILED,
                AgentState.LIMIT_REACHED,
                AgentState.INTERRUPTED,
            }:
                try:
                    self.save_checkpoint()
                except CheckpointError as error:
                    self.console.print(f"[red]Checkpoint error: {error}[/red]")

    def save_checkpoint(self) -> Path:
        """Persist the current terminal state and return the checkpoint path."""
        if self.checkpoint_store is None or self.session_id is None:
            raise CheckpointError("checkpoint persistence is not configured")
        return self.checkpoint_store.save(self, self.session_id)

    def restore_checkpoint(self, restored: RestoredCheckpoint) -> None:
        """Apply validated state without replaying model or tool side effects."""
        payload = restored.payload
        state = payload["agent"]
        usage = state["budget"]
        self.session_id = restored.session_id
        self.model = state["model"]
        self.messages = state["messages"]
        self.task_summary = state["task_summary"]
        self.runtime = AgentStateMachine.from_snapshot(state["runtime"])
        self.context_manager = ContextManager(
            self.model, budget=ContextBudget(**usage["limits"])
        )
        stats = usage["last_context_stats"]
        self.last_context_stats = ContextStats(**stats) if stats is not None else None
        self.last_prompt_tokens = usage["last_prompt_tokens"]
        self.total_prompt_tokens = usage["total_prompt_tokens"]
        self.total_completion_tokens = usage["total_completion_tokens"]
        self.permission_engine.restore(payload["permissions"])
        self.tool_registry.restore_traces(payload["tool_traces"])

    def _stream_response(self) -> tuple[str, list[dict]]:
        """Stream one completion, printing text as it arrives.

        Tool-call deltas arrive fragmented across chunks and are merged by
        index: ids/names/argument strings are concatenated.
        """
        schemas = self.tool_registry.schemas()
        prepared = self.context_manager.prepare(self.messages, schemas)
        self.last_context_stats = prepared.stats
        self.task_summary = prepared.task_summary
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=prepared.messages,
            tools=schemas,
            max_tokens=self.context_manager.budget.max_output_tokens,
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
        # Permission decisions are owned by the PermissionEngine (M3); the
        # per-tool requires_confirmation flag is no longer consulted here.
        policy = self.permission_engine.evaluate(name, args)
        if policy.decision is Decision.DENY:
            self.permission_engine.record(name, policy, "policy_deny")
            result = ToolResult.error(
                ToolErrorCode.PERMISSION_DENIED,
                f"permission denied by policy: {policy.reason}",
            )
            self.tool_registry.record_result(
                name, args, result, tool_call.get("id")
            )
            return result.to_model_text()
        if policy.decision is Decision.ASK:
            self.runtime.transition(AgentState.WAITING_APPROVAL, f"approval required for {name}")
            outcome = self._request_approval(policy)
            self.runtime.transition(
                AgentState.EXECUTING_TOOLS,
                f"approval outcome {outcome} for {name}",
            )
            self.permission_engine.record(name, policy, outcome)
            if outcome == "allow_session":
                self.permission_engine.approve_session(policy)
            if outcome == "deny":
                result = ToolResult.error(
                    ToolErrorCode.PERMISSION_DENIED,
                    "permission denied by user",
                )
                self.tool_registry.record_result(
                    name, args, result, tool_call.get("id")
                )
                return result.to_model_text()
        else:
            # Pure policy allows carry no grant key; an ALLOW with a grant key
            # means a session grant short-circuited the prompt.
            outcome = "session_grant_hit" if policy.grant_key else "policy_allow"
            self.permission_engine.record(name, policy, outcome)
            if policy.grant_key:
                self.console.print(f"[dim]{policy.reason}[/dim]")
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

    def _request_approval(self, policy: PolicyDecision) -> str:
        """Ask the user about one ASK decision; the default answer is deny.

        Returns the audit outcome: "allow_once", "allow_session", or "deny".
        The prompt shows the engine's normalized target, never the raw model
        arguments, so the user approves exactly what was classified.
        """
        # Non-interactive callers (the eval harness) inject a handler so runs
        # never block on stdin; the handler sees the same normalized policy.
        if self.approval_handler is not None:
            return self.approval_handler(policy)
        self.console.print(f"[yellow]Approval needed:[/yellow] {policy.reason}")
        self.console.print(f"[yellow]Target: {policy.target}[/yellow]")
        if not sys.stdin.isatty():
            self.console.print("[yellow]stdin is not a TTY; denying automatically.[/yellow]")
            return "deny"
        if policy.session_grantable:
            prompt = "Allow? [y] once / [s] this kind for the session / [N] deny "
        else:
            prompt = "Allow? [y] once / [N] deny "
        try:
            answer = input(prompt)
        except EOFError:
            return "deny"
        answer = answer.strip().lower()
        if answer in ("y", "yes"):
            return "allow_once"
        if answer in ("s", "session") and policy.session_grantable:
            return "allow_session"
        return "deny"

def _summarize(name: str, args: dict) -> str:
    """One-line summary of a tool call: the path, the command, or the args."""
    text = args.get("command") if name == "run_bash" else args.get("path")
    if text is None:
        text = json.dumps(args, ensure_ascii=False)
    text = str(text).replace("\n", " ")
    return text if len(text) <= 80 else text[:77] + "..."
