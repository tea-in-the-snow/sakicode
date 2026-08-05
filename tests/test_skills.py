"""Tests for the skill system: indexing, overrides, loading, and safety."""

import os
from pathlib import Path

import pytest

from sakicode.permissions import Decision, PermissionEngine
from sakicode.skills import (
    SkillError,
    SkillLibrary,
    SkillScope,
    build_skill_tool,
)
from sakicode.tooling import ToolErrorCode, ToolRegistry


def make_skill(
    scope_root: Path,
    dirname: str,
    *,
    name: str | None = None,
    description: str = "Does something useful.",
    body: str = "Follow these steps carefully.",
    frontmatter_extra: str = "",
    resources: dict[str, str] | None = None,
) -> Path:
    skill_dir = scope_root / dirname
    skill_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = f"name: {name or dirname}\ndescription: {description}\n"
    (skill_dir / "SKILL.md").write_text(
        f"---\n{frontmatter}{frontmatter_extra}---\n\n{body}\n",
        encoding="utf-8",
    )
    for relative, content in (resources or {}).items():
        resource = skill_dir / relative
        resource.parent.mkdir(parents=True, exist_ok=True)
        resource.write_text(content, encoding="utf-8")
    return skill_dir


@pytest.fixture
def scopes(tmp_path):
    builtin = tmp_path / "builtin"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for root in (builtin, user, project):
        root.mkdir()
    return {
        "builtin": builtin,
        "user": user,
        "project": project,
        "workspace": tmp_path / "workspace",
    }


def discover(scopes) -> SkillLibrary:
    workspace = scopes["workspace"]
    workspace.mkdir(exist_ok=True)
    project_skills = workspace / ".sakicode" / "skills"
    project_skills.mkdir(parents=True, exist_ok=True)
    # Point the project scope at the fixture's "project" root by renaming.
    os.rmdir(project_skills)
    os.rename(scopes["project"], project_skills)
    scopes["project"] = project_skills
    return SkillLibrary.discover(
        workspace,
        user_dir=scopes["user"],
        builtin_dir=scopes["builtin"],
    )


def test_discovery_builds_a_lightweight_metadata_index(scopes):
    body_marker = "SECRET-BODY-MARKER"
    make_skill(scopes["project"], "deploy", body=f"Steps. {body_marker}")
    library = discover(scopes)

    skills = library.skills()
    assert [m.name for m in skills] == ["deploy"]
    assert skills[0].scope is SkillScope.PROJECT
    index = library.render_prompt_index()
    assert "deploy" in index and "Does something useful." in index
    assert "use_skill" in index
    # Progressive disclosure: the body never enters the prompt index.
    assert body_marker not in index


def test_bodies_are_read_at_load_time_not_index_time(scopes):
    make_skill(scopes["project"], "deploy", body="version one")
    library = discover(scopes)
    skill_md = library.get("deploy").path
    skill_md.write_text(
        "---\nname: deploy\ndescription: d\n---\n\nversion two\n",
        encoding="utf-8",
    )
    assert library.load_body("deploy") == "version two"
    # ... and the result is then cached.
    assert library.load_body("deploy") == "version two"


def test_project_scope_shadows_user_and_builtin(scopes):
    make_skill(scopes["builtin"], "review", description="builtin version")
    make_skill(scopes["user"], "review", description="user version")
    make_skill(scopes["project"], "review", description="project version")
    library = discover(scopes)

    skills = library.skills()
    assert len(skills) == 1
    assert skills[0].description == "project version"
    assert skills[0].scope is SkillScope.PROJECT
    shadowed = [d for d in library.diagnostics if d.kind == "shadowed"]
    assert {d.scope for d in shadowed} == {SkillScope.BUILTIN, SkillScope.USER}


def test_user_scope_shadows_builtin(scopes):
    make_skill(scopes["builtin"], "review", description="builtin version")
    make_skill(scopes["user"], "review", description="user version")
    library = discover(scopes)
    assert library.skills()[0].description == "user version"
    assert library.skills()[0].scope is SkillScope.USER


def test_missing_or_unclosed_frontmatter_is_invalid(scopes):
    skill_dir = scopes["project"] / "broken"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
    unclosed = scopes["project"] / "unclosed"
    unclosed.mkdir()
    (unclosed / "SKILL.md").write_text(
        "---\nname: unclosed\ndescription: never closed\n", encoding="utf-8"
    )
    library = discover(scopes)

    assert library.skills() == []
    invalid = [d for d in library.diagnostics if d.kind == "invalid"]
    assert len(invalid) == 2


@pytest.mark.parametrize(
    "frontmatter_extra, name, description",
    [
        ("", "Bad Name", "x"),
        ("", "valid-name", ""),
        ("", "valid-name", "x" * 2000),
        ("name: first\nname: second\n", "first", "x"),
        ("1bad: x\n", "valid-name", "x"),
        ("novalue\n", "valid-name", "x"),
    ],
)
def test_malicious_or_sloppy_metadata_is_rejected(
    scopes, frontmatter_extra, name, description
):
    make_skill(
        scopes["project"],
        "evil",
        name=name,
        description=description,
        frontmatter_extra=frontmatter_extra,
    )
    library = discover(scopes)
    assert library.skills() == []
    assert [d.kind for d in library.diagnostics] == ["invalid"]


def test_oversized_frontmatter_is_rejected(scopes):
    make_skill(
        scopes["project"],
        "bloated",
        frontmatter_extra=f"notes: {'x' * 5000}\n",
    )
    library = discover(scopes)
    assert library.skills() == []
    assert "frontmatter" in library.diagnostics[0].message


def test_duplicate_name_within_one_scope_keeps_the_first(scopes):
    make_skill(scopes["project"], "aaa", name="dup", description="first")
    make_skill(scopes["project"], "zzz", name="dup", description="second")
    library = discover(scopes)

    assert [m.description for m in library.skills()] == ["first"]
    assert [d.kind for d in library.diagnostics] == ["duplicate"]


def test_skill_dir_symlink_escaping_scope_is_rejected(scopes, tmp_path):
    outside = tmp_path / "outside"
    make_skill(tmp_path, "outside", name="escape")
    os.symlink(outside, scopes["project"] / "sneaky")
    library = discover(scopes)

    assert library.skills() == []
    assert [d.kind for d in library.diagnostics] == ["out_of_scope"]


def test_load_body_strips_frontmatter_and_rejects_oversize(scopes):
    make_skill(scopes["project"], "deploy", body="line one\nline two")
    make_skill(scopes["project"], "huge", body="x" * (70 * 1024))
    library = discover(scopes)

    assert library.load_body("deploy") == "line one\nline two"
    with pytest.raises(SkillError, match="limit"):
        library.load_body("huge")


def test_unknown_skill_raises_not_found(scopes):
    library = discover(scopes)
    with pytest.raises(SkillError, match="unknown skill"):
        library.load_body("nope")


def test_resources_are_listed_and_read_within_the_skill_dir(scopes):
    make_skill(
        scopes["project"],
        "deploy",
        resources={"notes/checklist.md": "check this", "run.sh": "echo hi"},
    )
    library = discover(scopes)

    assert library.list_resources("deploy") == ["notes/checklist.md", "run.sh"]
    assert library.read_resource("deploy", "notes/checklist.md") == "check this"


@pytest.mark.parametrize("resource", ["../secret.txt", "notes/../../secret.txt", "/etc/passwd"])
def test_resource_paths_cannot_escape_the_skill_dir(scopes, resource):
    make_skill(scopes["project"], "deploy", resources={"notes/a.md": "a"})
    library = discover(scopes)
    with pytest.raises(SkillError, match="escapes|relative"):
        library.read_resource("deploy", resource)


def test_resource_symlink_escaping_skill_dir_is_rejected(scopes, tmp_path):
    skill_dir = make_skill(scopes["project"], "deploy")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    os.symlink(secret, skill_dir / "leak.txt")
    library = discover(scopes)

    assert "leak.txt" not in library.list_resources("deploy")
    with pytest.raises(SkillError, match="escapes"):
        library.read_resource("deploy", "leak.txt")


def test_use_skill_tool_round_trip_through_registry(scopes):
    make_skill(
        scopes["project"],
        "deploy",
        body="THE FULL INSTRUCTIONS",
        resources={"checklist.md": "THE CHECKLIST"},
    )
    library = discover(scopes)
    registry = ToolRegistry([build_skill_tool(library)])

    result = registry.execute("use_skill", {"name": "deploy"})
    assert not result.is_error
    assert result.content == "THE FULL INSTRUCTIONS"
    assert result.metadata == {"skill": "deploy", "scope": "project", "kind": "body"}

    resource = registry.execute(
        "use_skill", {"name": "deploy", "resource": "checklist.md"}
    )
    assert resource.content == "THE CHECKLIST"
    assert registry.traces[-1].tool_name == "use_skill"


def test_use_skill_tool_validates_arguments_and_names(scopes):
    make_skill(scopes["project"], "deploy")
    library = discover(scopes)
    registry = ToolRegistry([build_skill_tool(library)])

    missing = registry.execute("use_skill", {})
    assert missing.is_error
    assert missing.error_code is ToolErrorCode.INVALID_ARGUMENTS

    unknown = registry.execute("use_skill", {"name": "nope"})
    assert unknown.is_error
    assert unknown.error_code is ToolErrorCode.INVALID_ARGUMENTS
    assert "deploy" in unknown.content  # lists available skills


def test_use_skill_tool_reports_traversal_as_structured_error(scopes):
    make_skill(scopes["project"], "deploy")
    library = discover(scopes)
    registry = ToolRegistry([build_skill_tool(library)])

    result = registry.execute(
        "use_skill", {"name": "deploy", "resource": "../../outside.txt"}
    )
    assert result.is_error
    assert result.error_code is ToolErrorCode.IO_ERROR
    assert "escapes" in result.content


def test_permission_engine_allows_confined_skill_reads(tmp_path):
    engine = PermissionEngine(tmp_path)
    policy = engine.evaluate("use_skill", {"name": "deploy"})
    assert policy.decision is Decision.ALLOW
    assert "skill" in policy.reason


def test_packaged_builtin_skill_is_discovered(tmp_path):
    library = SkillLibrary.discover(
        tmp_path / "workspace", user_dir=tmp_path / "no-user-skills"
    )
    review = library.get("code-review")
    assert review is not None
    assert review.scope is SkillScope.BUILTIN
    assert "checklist.md" in library.list_resources("code-review")
    assert "severity" in library.load_body("code-review")


def test_empty_library_renders_no_prompt_section(scopes):
    library = discover(scopes)
    assert library.skills() == []
    assert library.render_prompt_index() == ""
