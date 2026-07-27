"""Built-in repository tools registered through the shared tool protocol."""

import glob as glob_module
import os
import re
import subprocess
from typing import Any

from .tooling import (
    FunctionTool,
    ToolErrorCode,
    ToolRegistry,
    ToolResult,
)

READ_FILE_MAX_LINES = 2000
RUN_BASH_TIMEOUT = 60  # seconds
RUN_BASH_MAX_CHARS = 4000
SEARCH_MAX_RESULTS = 100
GREP_MAX_FILE_BYTES = 1_000_000  # skip files larger than 1 MB
GREP_SKIP_DIRS = {".git", ".venv", "__pycache__", "node_modules"}


def read_file(path: str) -> ToolResult:
    """Return a file's contents with 1-based line numbers."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as file:
            lines = file.read().splitlines()
    except OSError as error:
        return ToolResult.error(ToolErrorCode.IO_ERROR, str(error), path=path)
    if not lines:
        return ToolResult.success("(empty file)", path=path, total_lines=0)
    shown = lines[:READ_FILE_MAX_LINES]
    output = "\n".join(
        f"{index}\t{line}" for index, line in enumerate(shown, start=1)
    )
    truncated = len(lines) > READ_FILE_MAX_LINES
    if truncated:
        output += (
            f"\n... truncated: showing {READ_FILE_MAX_LINES} "
            f"of {len(lines)} lines"
        )
    return ToolResult.success(
        output,
        path=path,
        truncated=truncated,
        shown_lines=len(shown),
        total_lines=len(lines),
    )


def write_file(path: str, content: str) -> ToolResult:
    """Create or overwrite a file (parent directories are created)."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as file:
            file.write(content)
    except OSError as error:
        return ToolResult.error(ToolErrorCode.IO_ERROR, str(error), path=path)
    return ToolResult.success(
        f"Wrote {len(content)} characters to {path}",
        path=path,
        characters_written=len(content),
    )


def edit_file(path: str, old_string: str, new_string: str) -> ToolResult:
    """Replace the unique occurrence of old_string with new_string."""
    try:
        with open(path, "r", encoding="utf-8") as file:
            content = file.read()
    except OSError as error:
        return ToolResult.error(ToolErrorCode.IO_ERROR, str(error), path=path)
    count = content.count(old_string)
    if count == 0:
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"old_string not found in {path}",
            path=path,
            occurrences=0,
        )
    if count > 1:
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"old_string occurs {count} times in {path}; it must be unique",
            path=path,
            occurrences=count,
        )
    try:
        with open(path, "w", encoding="utf-8") as file:
            file.write(content.replace(old_string, new_string, 1))
    except OSError as error:
        return ToolResult.error(ToolErrorCode.IO_ERROR, str(error), path=path)
    return ToolResult.success(f"Edited {path}", path=path, replacements=1)


def run_bash(command: str) -> ToolResult:
    """Run a bash command in the cwd where sakicode was started."""
    try:
        process = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=RUN_BASH_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        partial_output, _, _ = _truncate(
            _subprocess_output(error), RUN_BASH_MAX_CHARS
        )
        message = f"command timed out after {RUN_BASH_TIMEOUT}s"
        if partial_output:
            message += f"\nPartial output:\n{partial_output}"
        return ToolResult.error(
            ToolErrorCode.TIMEOUT,
            message,
            timeout_seconds=RUN_BASH_TIMEOUT,
        )
    output = ((process.stdout or "") + (process.stderr or "")).strip()
    if not output:
        output = "(no output)"
    output, truncated, original_chars = _truncate(output, RUN_BASH_MAX_CHARS)
    metadata = {
        "exit_code": process.returncode,
        "truncated": truncated,
        "original_chars": original_chars,
        "shown_chars": min(original_chars, RUN_BASH_MAX_CHARS),
    }
    if process.returncode != 0:
        return ToolResult.error(
            ToolErrorCode.NON_ZERO_EXIT,
            output,
            **metadata,
        )
    return ToolResult.success(output, **metadata)


def glob(pattern: str, path: str = ".") -> ToolResult:
    """Find files matching a glob pattern under path."""
    matches = glob_module.glob(os.path.join(path, pattern), recursive=True)
    files = sorted(match for match in matches if os.path.isfile(match))
    if not files:
        return ToolResult.success(
            "No matches",
            truncated=False,
            total_matches=0,
            shown_matches=0,
        )
    shown = files[:SEARCH_MAX_RESULTS]
    output = "\n".join(shown)
    truncated = len(files) > SEARCH_MAX_RESULTS
    if truncated:
        output += (
            f"\n... truncated: showing {SEARCH_MAX_RESULTS} "
            f"of {len(files)} matches"
        )
    return ToolResult.success(
        output,
        truncated=truncated,
        total_matches=len(files),
        shown_matches=len(shown),
    )


def grep(pattern: str, path: str = ".") -> ToolResult:
    """Regex-search files under path and return matching lines."""
    try:
        regex = re.compile(pattern)
    except re.error as error:
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENTS,
            f"invalid regex: {error}",
        )
    candidates = _grep_candidates(path)
    results = []
    for file_path in candidates:
        try:
            if os.path.getsize(file_path) > GREP_MAX_FILE_BYTES:
                continue
            with open(
                file_path, "r", encoding="utf-8", errors="replace"
            ) as file:
                text = file.read()
        except OSError:
            continue
        if "\0" in text:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                results.append(f"{file_path}:{line_number}: {line}")
                if len(results) >= SEARCH_MAX_RESULTS:
                    return ToolResult.success(
                        "\n".join(results)
                        + f"\n... truncated at {SEARCH_MAX_RESULTS} matches",
                        truncated=True,
                        shown_matches=len(results),
                    )
    return ToolResult.success(
        "\n".join(results) if results else "No matches",
        truncated=False,
        shown_matches=len(results),
    )


def _grep_candidates(path: str) -> list[str]:
    if os.path.isfile(path):
        return [path]
    candidates = []
    for root, directories, files in os.walk(path):
        directories[:] = [
            directory
            for directory in directories
            if directory not in GREP_SKIP_DIRS
        ]
        candidates.extend(os.path.join(root, file) for file in files)
    return sorted(candidates)


def _truncate(text: str, limit: int) -> tuple[str, bool, int]:
    original_chars = len(text)
    if original_chars <= limit:
        return text, False, original_chars
    return text[:limit] + "\n... output truncated", True, original_chars


def _subprocess_output(error: subprocess.TimeoutExpired) -> str:
    parts = []
    for output in (error.stdout, error.stderr):
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        if output:
            parts.append(output)
    return "".join(parts).strip()


def _object_schema(
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


# Note: requires_confirmation remains part of the M2 Tool protocol, but since
# M3 the Agent no longer reads it — allow/ask/deny decisions are owned by
# permissions.PermissionEngine. The flag is kept so the protocol stays stable.
BUILTIN_TOOLS = [
    FunctionTool(
        name="read_file",
        description="Read a file's contents with line numbers (max 2000 lines).",
        input_schema=_object_schema(
            {"path": {"type": "string", "description": "Path to the file."}},
            ["path"],
        ),
        handler=read_file,
    ),
    FunctionTool(
        name="write_file",
        description="Create or overwrite a file with the given content.",
        input_schema=_object_schema(
            {
                "path": {"type": "string", "description": "Path to the file."},
                "content": {
                    "type": "string",
                    "description": "Full file content.",
                    "x-sensitive": True,
                },
            },
            ["path", "content"],
        ),
        handler=write_file,
        requires_confirmation=True,
    ),
    FunctionTool(
        name="edit_file",
        description="Replace a unique exact string in a file.",
        input_schema=_object_schema(
            {
                "path": {"type": "string", "description": "Path to the file."},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to replace; must occur once.",
                    "x-sensitive": True,
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text.",
                    "x-sensitive": True,
                },
            },
            ["path", "old_string", "new_string"],
        ),
        handler=edit_file,
        requires_confirmation=True,
    ),
    FunctionTool(
        name="run_bash",
        description="Run a bash command (60s timeout) and return stdout+stderr.",
        input_schema=_object_schema(
            {
                "command": {
                    "type": "string",
                    "description": "The bash command to run.",
                    "x-sensitive": True,
                }
            },
            ["command"],
        ),
        handler=run_bash,
        requires_confirmation=True,
    ),
    FunctionTool(
        name="glob",
        description="Find files matching a glob pattern (e.g. '**/*.py').",
        input_schema=_object_schema(
            {
                "pattern": {"type": "string", "description": "Glob pattern."},
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: cwd).",
                    "default": ".",
                },
            },
            ["pattern"],
        ),
        handler=glob,
    ),
    FunctionTool(
        name="grep",
        description=(
            "Regex-search file contents; returns matching lines as "
            "'file:line: text'."
        ),
        input_schema=_object_schema(
            {
                "pattern": {
                    "type": "string",
                    "description": "Python regex pattern.",
                },
                "path": {
                    "type": "string",
                    "description": "File or directory to search (default: cwd).",
                    "default": ".",
                },
            },
            ["pattern"],
        ),
        handler=grep,
    ),
]

def create_registry() -> ToolRegistry:
    """Create an isolated registry so each Agent owns its trace history."""
    return ToolRegistry(BUILTIN_TOOLS)


DEFAULT_REGISTRY = create_registry()

# Temporary compatibility aliases for callers that only discover schemas.
TOOL_SCHEMAS = DEFAULT_REGISTRY.schemas()
REQUIRES_CONFIRMATION = {
    tool.name for tool in BUILTIN_TOOLS if tool.requires_confirmation
}


def dispatch(name: str, arguments: dict[str, Any]) -> ToolResult:
    """Validate and invoke a built-in tool through the default registry."""
    return DEFAULT_REGISTRY.execute(name, arguments)
