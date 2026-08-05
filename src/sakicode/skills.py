"""Declarative skills: indexed at startup, loaded progressively on demand.

A skill is a directory containing ``SKILL.md``: a tiny frontmatter block
(``name``, ``description``) followed by a Markdown body of instructions,
plus optional bundled resource files. Discovery parses frontmatter only, so
the system prompt carries a lightweight name/description index; bodies and
resources are read only when the model activates a skill through the
``use_skill`` tool. Scopes are builtin < user < project: a higher scope
shadows a lower-scope skill of the same name, and every override or rejected
file is recorded as a diagnostic instead of failing silently.

Skill content is prompt supply chain: it becomes model instructions, so all
metadata is validated and capped, and every path is resolved and confined to
its skill directory before any byte is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import re
from typing import Any

from .tooling import FunctionTool, ToolErrorCode, ToolResult

USE_SKILL_TOOL_NAME = "use_skill"

_NAME_PATTERN = re.compile(r"^[a-z0-9](?:-?[a-z0-9]){0,63}$")
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_MAX_FRONTMATTER_BYTES = 4_096
_MAX_DESCRIPTION_CHARS = 1_024
_MAX_BODY_BYTES = 64 * 1_024
_MAX_RESOURCE_BYTES = 64 * 1_024
_MAX_RESOURCES = 50
_MAX_SKILLS_PER_SCOPE = 100


class SkillScope(str, Enum):
    BUILTIN = "builtin"
    USER = "user"
    PROJECT = "project"


# Scanned in ascending priority: a later (higher) scope overwrites winners.
_SCAN_ORDER = (SkillScope.BUILTIN, SkillScope.USER, SkillScope.PROJECT)


class SkillError(Exception):
    """A skill could not be parsed, found, or safely read."""


class SkillNotFoundError(SkillError):
    """The requested skill name is not in the index."""


@dataclass(frozen=True)
class SkillMetadata:
    """The lightweight index entry: everything known without reading the body."""

    name: str
    description: str
    scope: SkillScope
    path: Path  # resolved SKILL.md path


@dataclass(frozen=True)
class SkillDiagnostic:
    """A non-fatal discovery problem or an override, kept for inspection."""

    kind: str  # invalid | duplicate | shadowed | out_of_scope
    scope: SkillScope
    path: str
    message: str


@dataclass
class _SkillEntry:
    metadata: SkillMetadata
    skill_dir: Path  # resolved directory containing SKILL.md


class SkillLibrary:
    """Discover skills across scopes and load their content on demand."""

    def __init__(self) -> None:
        self._entries: dict[str, _SkillEntry] = {}
        self._diagnostics: list[SkillDiagnostic] = []
        self._body_cache: dict[str, str] = {}

    @classmethod
    def discover(
        cls,
        workspace_root: Path,
        *,
        user_dir: Path | None = None,
        builtin_dir: Path | None = None,
    ) -> SkillLibrary:
        """Build the index from the builtin, user and project scope roots."""
        roots = {
            SkillScope.BUILTIN: builtin_dir
            if builtin_dir is not None
            else Path(__file__).resolve().parent / "builtin_skills",
            SkillScope.USER: user_dir
            if user_dir is not None
            else Path.home() / ".sakicode" / "skills",
            SkillScope.PROJECT: Path(workspace_root) / ".sakicode" / "skills",
        }
        library = cls()
        for scope in _SCAN_ORDER:
            library._scan_scope(scope, roots[scope])
        return library

    def skills(self) -> list[SkillMetadata]:
        """Active skills (winners after scope overrides), sorted by name."""
        return sorted(
            (entry.metadata for entry in self._entries.values()),
            key=lambda metadata: metadata.name,
        )

    @property
    def diagnostics(self) -> list[SkillDiagnostic]:
        return list(self._diagnostics)

    def get(self, name: str) -> SkillMetadata | None:
        entry = self._entries.get(name)
        return entry.metadata if entry is not None else None

    def load_body(self, name: str) -> str:
        """Read and cache a skill's instruction body (frontmatter excluded)."""
        entry = self._require(name)
        if name not in self._body_cache:
            _, body = _parse_skill_file(entry.metadata.path, _MAX_BODY_BYTES)
            if not body:
                raise SkillError(f"skill {name!r} has an empty body")
            self._body_cache[name] = body
        return self._body_cache[name]

    def list_resources(self, name: str) -> list[str]:
        """Relative paths of files bundled with the skill (SKILL.md excluded)."""
        entry = self._require(name)
        resources = []
        for path in sorted(entry.skill_dir.rglob("*")):
            if len(resources) >= _MAX_RESOURCES:
                break
            if not path.is_file():
                continue
            # A symlinked file must not escape the skill directory either.
            if not path.resolve().is_relative_to(entry.skill_dir):
                continue
            relative = path.relative_to(entry.skill_dir).as_posix()
            if relative != "SKILL.md":
                resources.append(relative)
        return resources

    def read_resource(self, name: str, resource: str) -> str:
        """Read one bundled resource, confined to the skill directory."""
        entry = self._require(name)
        candidate = Path(resource)
        if candidate.is_absolute():
            raise SkillError("resource path must be relative")
        resolved = (entry.skill_dir / candidate).resolve()
        if not resolved.is_relative_to(entry.skill_dir):
            raise SkillError(
                f"resource {resource!r} escapes the skill directory"
            )
        if not resolved.is_file():
            raise SkillError(f"skill {name!r} has no resource {resource!r}")
        return _read_capped(resolved, _MAX_RESOURCE_BYTES).decode(
            "utf-8", errors="replace"
        )

    def render_prompt_index(self) -> str:
        """The name/description index injected into the system prompt."""
        active = self.skills()
        if not active:
            return ""
        lines = [
            "# Available skills",
            "",
            "When a task matches a skill's description, call the "
            f"`{USE_SKILL_TOOL_NAME}` tool with its name to load the full "
            "instructions before acting, and again with a `resource` path to "
            "read a bundled file. Only descriptions are listed here; skill "
            "bodies are loaded on demand.",
            "",
        ]
        for metadata in active:
            lines.append(
                f"- `{metadata.name}` ({metadata.scope.value}): "
                f"{metadata.description}"
            )
        return "\n".join(lines)

    def _require(self, name: str) -> _SkillEntry:
        entry = self._entries.get(name)
        if entry is None:
            raise SkillNotFoundError(f"unknown skill {name!r}")
        return entry

    def _scan_scope(self, scope: SkillScope, root: Path) -> None:
        root = Path(root)
        if not root.is_dir():
            return
        resolved_root = root.resolve()
        seen_in_scope: set[str] = set()
        scanned = 0
        for child in sorted(root.iterdir()):
            if child.name.startswith("."):
                continue
            resolved_child = child.resolve()
            if not resolved_child.is_dir():
                continue
            if scanned >= _MAX_SKILLS_PER_SCOPE:
                self._diagnostics.append(
                    SkillDiagnostic(
                        "invalid",
                        scope,
                        str(child),
                        f"scope already holds {_MAX_SKILLS_PER_SCOPE} skills; "
                        "the rest are ignored",
                    )
                )
                break
            scanned += 1
            self._scan_skill(scope, resolved_root, resolved_child, seen_in_scope)

    def _scan_skill(
        self,
        scope: SkillScope,
        scope_root: Path,
        skill_dir: Path,
        seen_in_scope: set[str],
    ) -> None:
        if not skill_dir.is_relative_to(scope_root):
            self._diagnostics.append(
                SkillDiagnostic(
                    "out_of_scope",
                    scope,
                    str(skill_dir),
                    "skill directory is a symlink escaping the scope root",
                )
            )
            return
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            self._diagnostics.append(
                SkillDiagnostic(
                    "invalid", scope, str(skill_dir), "missing SKILL.md"
                )
            )
            return
        resolved_md = skill_md.resolve()
        if not resolved_md.is_relative_to(skill_dir):
            self._diagnostics.append(
                SkillDiagnostic(
                    "out_of_scope",
                    scope,
                    str(skill_md),
                    "SKILL.md is a symlink escaping the skill directory",
                )
            )
            return
        try:
            frontmatter, _ = _parse_frontmatter(resolved_md)
            metadata = _validate_metadata(scope, resolved_md, frontmatter)
        except SkillError as error:
            self._diagnostics.append(
                SkillDiagnostic("invalid", scope, str(resolved_md), str(error))
            )
            return
        if metadata.name in seen_in_scope:
            self._diagnostics.append(
                SkillDiagnostic(
                    "duplicate",
                    scope,
                    str(resolved_md),
                    f"skill name {metadata.name!r} is already defined in the "
                    f"{scope.value} scope; keeping the first",
                )
            )
            return
        seen_in_scope.add(metadata.name)
        existing = self._entries.get(metadata.name)
        if existing is not None:
            self._diagnostics.append(
                SkillDiagnostic(
                    "shadowed",
                    existing.metadata.scope,
                    str(existing.metadata.path),
                    f"shadowed by {scope.value} skill at {resolved_md}",
                )
            )
        self._entries[metadata.name] = _SkillEntry(metadata, skill_dir)
        self._body_cache.pop(metadata.name, None)


def build_skill_tool(library: SkillLibrary) -> FunctionTool:
    """Adapt progressive skill loading to the shared Tool protocol."""

    def use_skill(name: str, resource: str | None = None) -> ToolResult:
        try:
            if resource is None:
                body = library.load_body(name)
                metadata = library.get(name)
                return ToolResult.success(
                    body,
                    skill=name,
                    scope=metadata.scope.value if metadata else None,
                    kind="body",
                )
            content = library.read_resource(name, resource)
            return ToolResult.success(
                content, skill=name, resource=resource, kind="resource"
            )
        except SkillNotFoundError as error:
            available = ", ".join(m.name for m in library.skills()) or "(none)"
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENTS,
                f"{error}; available skills: {available}",
            )
        except SkillError as error:
            return ToolResult.error(ToolErrorCode.IO_ERROR, str(error))

    return FunctionTool(
        name=USE_SKILL_TOOL_NAME,
        description=(
            "Load a skill's full instructions by name, or read a file bundled "
            "with a skill by also passing its relative resource path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of a skill from the skill index.",
                },
                "resource": {
                    "type": "string",
                    "description": (
                        "Optional relative path of a resource bundled with "
                        "the skill, read instead of the instruction body."
                    ),
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=use_skill,
    )


def _validate_metadata(
    scope: SkillScope, path: Path, frontmatter: dict[str, str]
) -> SkillMetadata:
    name = frontmatter.get("name", "")
    if not _NAME_PATTERN.match(name):
        raise SkillError(
            f"invalid skill name {name!r}: lowercase letters, digits and "
            "single dashes, up to 64 characters"
        )
    description = frontmatter.get("description", "").strip()
    if not description:
        raise SkillError("frontmatter is missing a non-empty description")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        raise SkillError(
            f"description exceeds {_MAX_DESCRIPTION_CHARS} characters"
        )
    return SkillMetadata(
        name=name, description=description, scope=scope, path=path
    )


def _parse_skill_file(path: Path, max_bytes: int) -> tuple[dict[str, str], str]:
    raw = _read_capped(path, max_bytes).decode("utf-8", errors="replace")
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with a --- frontmatter block")
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise SkillError("frontmatter block is not closed by a --- line")
    return _parse_frontmatter_lines(lines[1:closing]), "\n".join(
        lines[closing + 1 :]
    ).strip()


def _parse_frontmatter(path: Path) -> tuple[dict[str, str], None]:
    """Read only the frontmatter block, line by line, with a byte cap."""
    lines: list[str] = []
    consumed = 0
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        for line in file:
            consumed += len(line.encode("utf-8"))
            if consumed > _MAX_FRONTMATTER_BYTES:
                raise SkillError(
                    f"frontmatter exceeds {_MAX_FRONTMATTER_BYTES} bytes"
                )
            lines.append(line.rstrip("\n"))
            if len(lines) > 1 and lines[-1].strip() == "---":
                if lines[0].strip() != "---":
                    raise SkillError(
                        "SKILL.md must start with a --- frontmatter block"
                    )
                return _parse_frontmatter_lines(lines[1:-1]), None
    raise SkillError("frontmatter block is not closed by a --- line")


def _parse_frontmatter_lines(lines: list[str]) -> dict[str, str]:
    if not lines:
        raise SkillError("frontmatter block is empty")
    frontmatter: dict[str, str] = {}
    for line in lines:
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or not _KEY_PATTERN.match(key):
            raise SkillError(f"malformed frontmatter line {line!r}")
        value = value.strip()
        if not value:
            raise SkillError(f"frontmatter key {key!r} has an empty value")
        if key in frontmatter:
            raise SkillError(f"duplicate frontmatter key {key!r}")
        frontmatter[key] = value
    return frontmatter


def _read_capped(path: Path, max_bytes: int) -> bytes:
    with open(path, "rb") as file:
        data = file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SkillError(f"{path.name} exceeds the {max_bytes}-byte limit")
    return data
