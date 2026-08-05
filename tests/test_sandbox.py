"""Tests for the M9 bubblewrap sandbox around approved shell commands."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from sakicode import evaluate, tools
from sakicode.evaluate import run_task
from sakicode.sandbox import (
    SandboxPolicy,
    bwrap_available,
    build_argv,
    scrub_environment,
)
from sakicode.tooling import ToolErrorCode

BWRAP = pytest.mark.skipif(not bwrap_available(), reason="bwrap not installed")


def _argv_tail(argv):
    separator = argv.index("--")
    return argv[:separator], argv[separator + 1:]


def test_build_argv_structure(tmp_path):
    policy = SandboxPolicy()
    argv = build_argv("echo hi", policy, tmp_path, tmp_path)
    prefix, command = _argv_tail(argv)

    assert command == ["bash", "-c", "echo hi"]
    assert prefix[0] == "bwrap"
    assert ["--ro-bind", "/", "/"] == prefix[1:4]
    # The private /tmp precedes the workspace bind so a workspace under /tmp
    # is not masked away.
    assert prefix.index("--tmpfs") < prefix.index("--bind")
    bind_at = prefix.index("--bind")
    assert prefix[bind_at + 1] == str(tmp_path.resolve())
    assert prefix[bind_at + 2] == str(tmp_path.resolve())
    assert "--unshare-net" in prefix
    assert "--die-with-parent" in prefix


def test_build_argv_network_opt_in(tmp_path):
    argv = build_argv("x", SandboxPolicy(network=True), tmp_path, tmp_path)
    assert "--unshare-net" not in argv


def test_build_argv_extra_writable(tmp_path):
    extra = tmp_path / "shared"
    extra.mkdir()
    argv = build_argv(
        "x", SandboxPolicy(extra_writable=(extra,)), tmp_path, tmp_path
    )
    bind_at = argv.index("--bind", argv.index(str(extra.resolve())) - 1)
    assert argv[bind_at + 1] == str(extra.resolve())


def test_build_argv_masks_credential_dirs(tmp_path, monkeypatch):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    argv = build_argv("x", SandboxPolicy(), tmp_path, tmp_path)
    tmpfs_targets = [
        argv[i + 1] for i, item in enumerate(argv) if item == "--tmpfs"
    ]
    assert str(ssh) in tmpfs_targets


def test_scrub_environment_removes_secrets():
    env = {
        "PATH": "/usr/bin",
        "HOME": "/home/u",
        "OPENAI_API_KEY": "sk-x",
        "AWS_SECRET_ACCESS_KEY": "s",
        "GITHUB_TOKEN": "t",
        "DB_PASSWORD": "p",
    }
    scrubbed = scrub_environment(SandboxPolicy(), env)
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/home/u"}


def test_create_registry_degrades_without_bwrap(monkeypatch, tmp_path):
    monkeypatch.setattr("sakicode.sandbox.bwrap_available", lambda: False)
    registry = tools.create_registry(
        sandbox_policy=SandboxPolicy(), workspace=tmp_path
    )
    result = registry.execute("run_bash", {"command": "echo plain"})
    assert not result.is_error
    assert result.metadata["sandbox"] == "none"


def test_registry_without_policy_is_untouched():
    registry = tools.create_registry()
    result = registry.execute("run_bash", {"command": "echo legacy"})
    assert not result.is_error
    assert result.metadata["sandbox"] == "none"


def test_eval_task_skips_when_bwrap_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "bwrap_available", lambda: False)
    task_dir = tmp_path / "tasks" / "needs-sandbox"
    (task_dir / "workspace").mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "prompt": "x",
                "requires": "bwrap",
                "checks": [{"type": "file_exists", "path": "notes.txt"}],
            }
        ),
        encoding="utf-8",
    )
    task = evaluate.load_tasks(tmp_path / "tasks")[0]
    client = SimpleNamespace()  # must never be called
    result = run_task(task, client, "m", tmp_path / "work")
    assert result.skipped is True
    assert result.final_state == "skipped"


def test_load_tasks_rejects_bad_sandbox_value(tmp_path):
    task_dir = tmp_path / "tasks" / "bad"
    (task_dir / "workspace").mkdir(parents=True)
    (task_dir / "task.json").write_text(
        json.dumps({"prompt": "x", "sandbox": "docker"}), encoding="utf-8"
    )
    with pytest.raises(evaluate.EvalError):
        evaluate.load_tasks(tmp_path / "tasks")


def _sandboxed_registry(tmp_path):
    return tools.create_registry(
        sandbox_policy=SandboxPolicy(), workspace=tmp_path
    )


@BWRAP
def test_sandbox_allows_workspace_write(tmp_path):
    registry = _sandboxed_registry(tmp_path)
    target = tmp_path / "out.txt"
    result = registry.execute(
        "run_bash", {"command": f"echo hi > {target}"}
    )
    assert not result.is_error
    assert result.metadata["sandbox"] == "bwrap"
    assert target.read_text() == "hi\n"


@BWRAP
def test_sandbox_denies_home_and_etc_writes(tmp_path):
    registry = _sandboxed_registry(tmp_path)
    home_canary = Path.home() / "sakicode-test-canary"
    assert not home_canary.exists()
    try:
        result = registry.execute(
            "run_bash", {"command": f"touch {home_canary}"}
        )
        assert result.is_error
        assert result.error_code is ToolErrorCode.NON_ZERO_EXIT
        assert not home_canary.exists()

        result = registry.execute(
            "run_bash", {"command": "touch /etc/sakicode-test-canary"}
        )
        assert result.is_error
        assert not Path("/etc/sakicode-test-canary").exists()
    finally:
        home_canary.unlink(missing_ok=True)


@BWRAP
def test_sandbox_hides_host_tmp(tmp_path):
    marker = Path("/tmp") / "sakicode-host-tmp-marker"
    marker.write_text("host only")
    try:
        registry = _sandboxed_registry(tmp_path)
        result = registry.execute("run_bash", {"command": f"cat {marker}"})
        assert result.is_error
    finally:
        marker.unlink()


@BWRAP
def test_sandbox_blocks_network(tmp_path):
    registry = _sandboxed_registry(tmp_path)
    result = registry.execute(
        "run_bash",
        {
            "command": "python3 -c \"import socket; "
            "socket.create_connection(('1.1.1.1', 443), timeout=3)\""
        },
    )
    assert result.is_error


@BWRAP
def test_sandbox_strips_secrets_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("SAKICODE_TEST_API_KEY", "super-secret")
    registry = _sandboxed_registry(tmp_path)
    result = registry.execute("run_bash", {"command": "env"})
    assert not result.is_error
    assert "SAKICODE_TEST_API_KEY" not in result.content
    assert "super-secret" not in result.content
    assert "PATH=" in result.content
