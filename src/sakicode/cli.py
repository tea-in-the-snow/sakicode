"""CLI entry point: parse flags, build the client and agent, start the REPL."""

import argparse
import os
import sys
from pathlib import Path

from openai import OpenAI
from rich.console import Console

from . import __version__
from . import tools as builtin_tools
from .agent import Agent
from .checkpoint import CheckpointError, CheckpointStore
from .config import load_config
from .mcp import DEFAULT_CONFIG_PATH, McpConfigError, connect_configured_servers
from .prompts import build_system_prompt
from .repl import run_repl
from .sandbox import SandboxPolicy, bwrap_available
from .skills import SkillLibrary, build_skill_tool


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sakicode",
        description="A minimal AI coding-agent CLI.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument("--model", help="model name (default: deepseek-chat)")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL")
    parser.add_argument(
        "--resume",
        metavar="SESSION_ID",
        help="resume a checkpointed session in the current workspace",
    )
    parser.add_argument(
        "--mcp-config",
        metavar="PATH",
        default=str(DEFAULT_CONFIG_PATH),
        help="MCP server config file (default: .sakicode/mcp.json if present)",
    )
    parser.add_argument(
        "--sandbox",
        choices=("auto", "bwrap", "off"),
        default=os.environ.get("SAKICODE_SANDBOX", "auto"),
        help="sandbox approved shell commands with bubblewrap "
        "(default: auto, env SAKICODE_SANDBOX)",
    )
    args = parser.parse_args()

    console = Console()
    config = load_config(model=args.model, base_url=args.base_url)
    if not config.api_key:
        console.print(
            "[red]Error: no API key found.[/red]\n"
            "Set the OPENAI_API_KEY environment variable (DEEPSEEK_API_KEY also works) "
            "and try again."
        )
        sys.exit(1)

    client = OpenAI(api_key=config.api_key, base_url=config.base_url)
    checkpoint_store = CheckpointStore(Path.cwd())
    sandbox_policy = None
    if args.sandbox != "off":
        if bwrap_available():
            sandbox_policy = SandboxPolicy()
            console.print(
                "[dim]Sandbox: bwrap (workspace-writable, no network, "
                "secrets scrubbed).[/dim]"
            )
        elif args.sandbox == "bwrap":
            console.print(
                "[red]--sandbox bwrap requested but no working bwrap binary "
                "was found on PATH.[/red]"
            )
            sys.exit(2)
        else:
            console.print(
                "[yellow]Sandbox degraded: bwrap not found; approved shell "
                "commands run with full user permissions.[/yellow]"
            )
    tool_registry = builtin_tools.create_registry(sandbox_policy=sandbox_policy)
    mcp_clients = []
    try:
        mcp_clients = connect_configured_servers(
            Path(args.mcp_config),
            tool_registry,
            on_error=lambda message: console.print(
                f"[yellow]MCP server skipped: {message}[/yellow]"
            ),
        )
    except McpConfigError as error:
        console.print(f"[red]MCP config error: {error}[/red]")
        sys.exit(2)
    for mcp_client in mcp_clients:
        console.print(
            f"[dim]MCP server {mcp_client.spec.name!r} connected "
            f"({mcp_client.server_info.get('name', 'unknown')}).[/dim]"
        )
    skill_library = SkillLibrary.discover(Path.cwd())
    for diagnostic in skill_library.diagnostics:
        console.print(
            f"[yellow]Skill {diagnostic.kind} "
            f"({diagnostic.scope.value}): {diagnostic.message}[/yellow]"
        )
    if skill_library.skills():
        tool_registry.register(build_skill_tool(skill_library))
        console.print(
            f"[dim]Skills indexed: "
            f"{', '.join(m.name for m in skill_library.skills())}.[/dim]"
        )
    agent = Agent(
        client=client,
        model=config.model,
        system_prompt=build_system_prompt(
            skill_index=skill_library.render_prompt_index()
        ),
        console=console,
        tool_registry=tool_registry,
        checkpoint_store=checkpoint_store,
        session_id=args.resume,
    )
    if args.resume:
        try:
            restored = checkpoint_store.load(args.resume)
            agent.restore_checkpoint(restored)
        except (CheckpointError, ValueError) as error:
            console.print(f"[red]Cannot resume session: {error}[/red]")
            sys.exit(2)
        note = (
            f" (migrated from schema v{restored.migrated_from})"
            if restored.migrated_from
            else ""
        )
        console.print(f"[green]Resumed session {agent.session_id}{note}.[/green]")
    else:
        console.print(f"[dim]Session: {agent.session_id}[/dim]")
    try:
        run_repl(agent, skill_library=skill_library)
    finally:
        for mcp_client in mcp_clients:
            mcp_client.close()


if __name__ == "__main__":
    main()
