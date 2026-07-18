"""CLI entry point: parse flags, build the client and agent, start the REPL."""

import argparse
import sys

from openai import OpenAI
from rich.console import Console

from . import __version__
from .agent import Agent
from .config import load_config
from .prompts import build_system_prompt
from .repl import run_repl


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
    agent = Agent(
        client=client,
        model=config.model,
        system_prompt=build_system_prompt(),
        console=console,
    )
    run_repl(agent)


if __name__ == "__main__":
    main()
