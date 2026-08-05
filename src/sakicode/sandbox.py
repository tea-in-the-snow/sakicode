"""M9: bubblewrap sandbox for approved shell commands.

The permission engine (M3) decides *whether* a bash command may run; the
sandbox limits *what it can reach* once approved. Every approved command runs
under bubblewrap with a read-only view of the whole filesystem, the workspace
bind-mounted writable at its real path, a private /tmp, no network by
default, and an environment scrubbed of API keys and other secrets.

Design notes:

- Policy is expressed as an argv prefix, so it is readable, testable, and
  needs no root and no daemon — bubblewrap is a single userspace binary that
  relies on unprivileged user namespaces (Linux only).
- Mounts apply in order: the private /tmp must come before the workspace
  bind, or a workspace living under /tmp would be masked away.
- Reads of $HOME stay visible (read-only), but well-known credential
  directories (~/.ssh, ~/.gnupg, ~/.aws, ~/.kube) are masked with empty
  tmpfs mounts: an approved command is otherwise an unclassified read
  channel straight into the model context.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess

# Environment names matching this pattern never enter the sandbox: an
# approved command could otherwise read OPENAI_API_KEY from `env` and (with
# network enabled) exfiltrate it.
DEFAULT_ENV_DENY = re.compile(
    r"API_?KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL", re.IGNORECASE
)

# Credential directories masked with an empty tmpfs inside the sandbox.
_MASKED_HOME_DIRS = (".ssh", ".gnupg", ".aws", ".kube")


@dataclass(frozen=True)
class SandboxPolicy:
    """What an approved command may reach inside the sandbox."""

    network: bool = False
    extra_writable: tuple[Path, ...] = ()
    env_deny: re.Pattern[str] = field(default=DEFAULT_ENV_DENY)


class SandboxUnavailableError(Exception):
    """bubblewrap was required but could not be found or executed."""


def bwrap_available() -> bool:
    """True when a working bwrap binary is on PATH."""
    path = shutil.which("bwrap")
    if path is None:
        return False
    try:
        probe = subprocess.run(
            [path, "--version"], capture_output=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def scrub_environment(
    policy: SandboxPolicy, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Return the process environment minus secret-looking variables."""
    source = os.environ if environ is None else environ
    return {
        key: value for key, value in source.items()
        if not policy.env_deny.search(key)
    }


def build_argv(
    command: str,
    policy: SandboxPolicy,
    workspace: Path,
    cwd: Path,
) -> list[str]:
    """Wrap ``bash -c command`` in the bubblewrap argv for the policy."""
    workspace = str(Path(workspace).resolve())
    argv = [
        "bwrap",
        "--ro-bind", "/", "/",
        # A private /tmp first: if the workspace itself lives under /tmp
        # (the eval harness does this), the later bind must win.
        "--tmpfs", "/tmp",
        "--bind", workspace, workspace,
    ]
    for extra in policy.extra_writable:
        resolved = str(Path(extra).resolve())
        argv += ["--bind", resolved, resolved]
    home = Path(os.environ.get("HOME", str(Path.home())))
    for name in _MASKED_HOME_DIRS:
        candidate = home / name
        if candidate.is_dir():
            argv += ["--tmpfs", str(candidate)]
    argv += ["--dev", "/dev", "--proc", "/proc"]
    if not policy.network:
        argv.append("--unshare-net")
    argv += [
        "--unshare-pid",
        "--new-session",
        "--die-with-parent",
        "--chdir", str(cwd),
        "--",
        "bash", "-c", command,
    ]
    return argv
