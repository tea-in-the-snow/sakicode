"""The interactive REPL: read user input, hand it to the agent, repeat."""

import json

from rich.console import Console

PROMPT = "[bold cyan]saki> [/bold cyan]"


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


def run_repl(agent, console: Console | None = None) -> None:
    console = console or agent.console
    console.print(
        "[dim]sakicode — use /runtime for states and /trace for tool calls; "
        "exit or quit to leave (Ctrl-C / Ctrl-D also work).[/dim]"
    )
    while True:
        try:
            user_input = console.input(PROMPT)
        except (EOFError, KeyboardInterrupt):
            console.print("\nBye!")
            return
        text = user_input.strip()
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            console.print("Bye!")
            return
        if text.lower() == "/runtime":
            console.print(format_runtime(agent.runtime))
            continue
        if text.lower() == "/trace":
            console.print(format_traces(agent.tool_registry))
            continue
        try:
            agent.run_turn(text)
        except KeyboardInterrupt:
            console.print("\n[yellow]Turn interrupted.[/yellow]")
