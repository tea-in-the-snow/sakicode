"""The interactive REPL: read user input, hand it to the agent, repeat."""

import json
import time
from pathlib import Path

from prompt_toolkit import ANSI, PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console

PROMPT_TEXT = ANSI("\x1b[1;36msaki> \x1b[0m")
HISTORY_PATH = Path.home() / ".cache" / "sakicode" / "history"


def format_runtime(runtime) -> str:
    """Format the state machine history for interactive inspection."""
    lines = [f"Runtime state: {runtime.state.value}"]
    if not runtime.history:
        lines.append("(no transitions yet)")
        return "\n".join(lines)
    for index, event in enumerate(runtime.history, start=1):
        lines.append(
            f"{index}. {event.previous.value} -> {event.current.value}: {event.reason}"
        )
    return "\n".join(lines)


def format_traces(tool_registry) -> str:
    """Format structured tool traces without exposing redacted arguments."""
    if not tool_registry.traces:
        return "Tool traces: (none)"
    lines = ["Tool traces:"]
    for index, trace in enumerate(tool_registry.traces, start=1):
        arguments = json.dumps(trace.arguments, ensure_ascii=False)
        outcome = "ok" if trace.ok else f"error:{trace.error_code}"
        lines.append(
            f"{index}. {trace.tool_name} [{outcome}] "
            f"{trace.duration_ms:.3f} ms args={arguments}"
        )
    return "\n".join(lines)


def format_approvals(permission_engine) -> str:
    """Format the permission audit log and the active session grants."""
    grants = permission_engine.session_grants
    lines = ["Session grants: " + (", ".join(grants) if grants else "(none)")]
    if not permission_engine.audit_log:
        lines.append("Approval audit: (no decisions yet)")
        return "\n".join(lines)
    lines.append("Approval audit:")
    for index, record in enumerate(permission_engine.audit_log, start=1):
        moment = time.strftime("%H:%M:%S", time.localtime(record.timestamp))
        lines.append(
            f"{index}. {moment} {record.tool} [{record.outcome}] "
            f"{record.target} — {record.reason}"
        )
    return "\n".join(lines)


def format_context(agent) -> str:
    """Show the latest four-layer context allocation."""
    stats = agent.last_context_stats
    if stats is None:
        return "Context budget: (no model request yet)"
    return (
        f"Context: {stats.estimated_input_tokens:,}/{stats.max_input_tokens:,} tokens "
        f"({stats.tokenizer})\n"
        f"instructions={stats.instruction_tokens:,}, task_state={stats.task_state_tokens:,}, "
        f"recent_dialogue={stats.recent_dialogue_tokens:,}, "
        f"tool_results={stats.tool_result_tokens:,}\n"
        f"compacted_groups={stats.dropped_groups}, "
        f"trimmed_tool_results={stats.trimmed_tool_results}"
    )


def format_toolbar(agent) -> str:
    """One-line status bar: runtime state and token usage."""
    state = agent.runtime.state.value
    total = agent.total_prompt_tokens + agent.total_completion_tokens
    return (
        f" state: {state} | context: ~{agent.context_tokens:,} tok "
        f"| total: {total:,} tok "
    )


def build_session(agent) -> PromptSession:
    """Create the prompt session: persistent history plus a status bar."""
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(HISTORY_PATH)),
        bottom_toolbar=lambda: format_toolbar(agent),
    )


def run_repl(agent, console: Console | None = None, session=None) -> None:
    console = console or agent.console
    session = session or build_session(agent)
    console.print(
        "[dim]sakicode — /runtime for states, /trace for tool calls, "
        "/approvals for permission decisions, /context for token layers; "
        "exit, quit or /exit to leave (Ctrl-C / Ctrl-D also work).[/dim]"
    )
    while True:
        try:
            user_input = session.prompt(PROMPT_TEXT)
        except UnicodeDecodeError:
            console.print(
                "[yellow]Could not decode terminal input as UTF-8; "
                "please re-enter the command.[/yellow]"
            )
            continue
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            return
        text = user_input.strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit", "/exit"):
            console.print("Bye!")
            return
        if text.lower() == "/runtime":
            console.print(format_runtime(agent.runtime))
            continue
        if text.lower() == "/trace":
            console.print(format_traces(agent.tool_registry))
            continue
        if text.lower() == "/approvals":
            console.print(format_approvals(agent.permission_engine))
            continue
        if text.lower() == "/context":
            console.print(format_context(agent))
            continue
        try:
            agent.run_turn(text)
        except KeyboardInterrupt:
            console.print("\n[yellow]Turn interrupted.[/yellow]")
