"""System prompt: a short base prompt plus ./AGENTS.md when present."""

import os

BASE_PROMPT = """\
You are sakicode, a coding-agent CLI running in the user's terminal.

Be concise. You have tools to read and edit files, run shell commands, and
search the codebase. Use them when they help answer the user's request, and
briefly explain what you are doing as you go. When you are done, summarize
the result in a sentence or two.\
"""


def build_system_prompt(cwd: str = ".") -> str:
    """Return the base prompt, with ./AGENTS.md appended if it exists."""
    prompt = BASE_PROMPT
    agents_md = os.path.join(cwd, "AGENTS.md")
    if os.path.isfile(agents_md):
        with open(agents_md, "r", encoding="utf-8") as f:
            prompt += f"\n\n# AGENTS.md\n\n{f.read()}"
    return prompt
