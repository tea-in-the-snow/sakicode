"""The interactive REPL: read user input, hand it to the agent, repeat."""

from rich.console import Console

PROMPT = "[bold cyan]saki> [/bold cyan]"


def run_repl(agent, console: Console | None = None) -> None:
    console = console or agent.console
    console.print("[dim]sakicode — type 'exit' or 'quit' to leave (Ctrl-C / Ctrl-D also work).[/dim]")
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
        try:
            agent.run_turn(text)
        except KeyboardInterrupt:
            console.print("\n[yellow]Turn interrupted.[/yellow]")
