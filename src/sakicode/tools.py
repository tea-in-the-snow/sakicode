"""The six tools sakicode exposes to the model, plus their schemas.

Every tool returns a string. Expected failures (missing file, ambiguous
edit, ...) are returned as "Error: ..." strings so the model can read them
and self-correct.
"""

import glob as glob_module
import os
import re
import subprocess

READ_FILE_MAX_LINES = 2000
RUN_BASH_TIMEOUT = 60  # seconds
RUN_BASH_MAX_CHARS = 4000
SEARCH_MAX_RESULTS = 100
GREP_MAX_FILE_BYTES = 1_000_000  # skip files larger than 1 MB
GREP_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}

# Tools that mutate state and therefore require interactive confirmation.
REQUIRES_CONFIRMATION = {"write_file", "edit_file", "run_bash"}


def read_file(path: str) -> str:
    """Return a file's contents with 1-based line numbers, capped at 2000 lines."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return f"Error: {e}"
    if not lines:
        return "(empty file)"
    shown = lines[:READ_FILE_MAX_LINES]
    out = "\n".join(f"{i}\t{line}" for i, line in enumerate(shown, start=1))
    if len(lines) > READ_FILE_MAX_LINES:
        out += f"\n... truncated: showing {READ_FILE_MAX_LINES} of {len(lines)} lines"
    return out


def write_file(path: str, content: str) -> str:
    """Create or overwrite a file (parent directories are created)."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"Error: {e}"
    return f"Wrote {len(content)} characters to {path}"


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace the unique occurrence of old_string with new_string."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return f"Error: {e}"
    count = content.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}"
    if count > 1:
        return f"Error: old_string occurs {count} times in {path}; it must be unique"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.replace(old_string, new_string, 1))
    except OSError as e:
        return f"Error: {e}"
    return f"Edited {path}"


def run_bash(command: str) -> str:
    """Run a bash command in the cwd where sakicode was started."""
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=RUN_BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {RUN_BASH_TIMEOUT}s"
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        output += f"\n(exit code: {proc.returncode})"
    output = output.strip()
    if not output:
        return "(no output)"
    if len(output) > RUN_BASH_MAX_CHARS:
        output = output[:RUN_BASH_MAX_CHARS] + "\n... output truncated"
    return output


def glob(pattern: str, path: str = ".") -> str:
    """Find files matching a glob pattern under path, sorted, capped at 100."""
    matches = glob_module.glob(os.path.join(path, pattern), recursive=True)
    files = sorted(m for m in matches if os.path.isfile(m))
    if not files:
        return "No matches"
    out = "\n".join(files[:SEARCH_MAX_RESULTS])
    if len(files) > SEARCH_MAX_RESULTS:
        out += f"\n... truncated: showing {SEARCH_MAX_RESULTS} of {len(files)} matches"
    return out


def grep(pattern: str, path: str = ".") -> str:
    """Regex-search files under path; return 'file:line: text' matches, capped at 100."""
    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"
    if os.path.isfile(path):
        candidates = [path]
    else:
        candidates = []
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in GREP_SKIP_DIRS]
            candidates.extend(os.path.join(root, f) for f in files)
    results = []
    for file_path in sorted(candidates):
        try:
            if os.path.getsize(file_path) > GREP_MAX_FILE_BYTES:
                continue
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if "\0" in text:  # binary file
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{file_path}:{lineno}: {line}")
                if len(results) >= SEARCH_MAX_RESULTS:
                    results.append(f"... truncated at {SEARCH_MAX_RESULTS} matches")
                    return "\n".join(results)
    return "\n".join(results) if results else "No matches"


# OpenAI function-calling schemas, passed as `tools=` on every request.
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents with line numbers (max 2000 lines).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file."},
                    "content": {"type": "string", "description": "Full file content."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a unique exact string in a file with a new string.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file."},
                    "old_string": {"type": "string", "description": "Exact text to replace (must occur exactly once)."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a bash command (60s timeout) and return stdout+stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command to run."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern (e.g. '**/*.py').",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern."},
                    "path": {"type": "string", "description": "Directory to search in (default: cwd)."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Regex-search file contents; returns matching lines as 'file:line: text'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex pattern."},
                    "path": {"type": "string", "description": "File or directory to search (default: cwd)."},
                },
                "required": ["pattern"],
            },
        },
    },
]

_FUNCTIONS = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "run_bash": run_bash,
    "glob": glob,
    "grep": grep,
}


def dispatch(name: str, arguments: dict) -> str:
    """Call the tool named `name` with `arguments` (already parsed from JSON)."""
    fn = _FUNCTIONS.get(name)
    if fn is None:
        return f"Error: unknown tool '{name}'"
    return fn(**arguments)
