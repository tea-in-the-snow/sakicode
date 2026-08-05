"""Fine-grained permission policy: allow/ask/deny, session grants, and audit.

The engine never trusts model-generated text. Every decision is computed from
normalized data only: paths are resolved against the workspace root (symlinks
and ``..`` included), and shell commands are whitespace-collapsed before being
classified or turned into grant keys.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
import re
import time


class Decision(str, Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class PolicyDecision:
    """The engine's verdict for one tool call, built from normalized data."""

    decision: Decision
    reason: str  # human-readable, audit-facing explanation
    target: str  # normalized target: resolved absolute path or command text
    grant_key: str | None  # stable session-grant key, from normalized data only
    session_grantable: bool  # whether "this kind for the session" is offered


@dataclass(frozen=True)
class ApprovalRecord:
    """One auditable disposition of a tool call."""

    tool: str
    target: str
    outcome: str  # policy_allow | policy_deny | allow_once | allow_session |
    # session_grant_hit | deny
    reason: str
    timestamp: float


_READ_TOOLS = frozenset({"read_file", "glob", "grep"})
_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
_BASH_TOOL = "run_bash"
_MCP_TOOL_PREFIX = "mcp__"
_SKILL_TOOLS = frozenset({"use_skill"})

# Session-grant keys. Read and write grants are class-level on purpose: a grant
# covers a *kind* of operation over normalized targets, never model prose. Bash
# grants are instead keyed by the exact normalized command text, so "this kind"
# for a shell command means that one command only.
_READ_OUTSIDE_GRANT = "read-outside"
_WORKSPACE_WRITE_GRANT = "workspace-write"

# Heuristic high-risk shell patterns. This list is deliberately conservative
# and can never be complete: it exists to deny unambiguous disasters by default,
# not to prove that anything unmatched is safe. Unmatched commands still ASK.
_HIGH_RISK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\brm\s+(?:-\w*[rf]\w*\s+)+\s*(?:/|/\*|~|\$HOME)(?:\s|$)"),
        "recursive/forced delete of the root or home directory",
    ),
    (re.compile(r"\bsudo\b"), "privilege escalation via sudo"),
    (re.compile(r"\bdd\b[^|;&]*\bof=/dev/"), "raw write to a device with dd"),
    (re.compile(r"\bmkfs(?:\.\w+)?\b"), "filesystem formatting"),
    (
        re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"),
        "fork bomb",
    ),
    (
        re.compile(r"\b(?:shutdown|reboot|halt|poweroff)\b"),
        "system power/state command",
    ),
    (
        re.compile(r"\b(?:curl|wget)\b[^|]*\|\s*(?:sudo\s+)?(?:bash|sh|zsh)\b"),
        "piping a remote download straight into a shell",
    ),
]

# Shell metacharacters that turn one command into a composite. Composite
# commands are never session-grantable: approving "ls" must never become a
# standing permission for "ls && rm -rf ...".
_COMPOSITE_TOKENS = ("&&", "||", ";", "|", ">", "<", "`", "$(", "\n")


def _has_composite_syntax(command: str) -> bool:
    return any(token in command for token in _COMPOSITE_TOKENS)


class PermissionEngine:
    """Classify tool calls, track session grants, and keep an audit log."""

    def __init__(self, workspace_root: Path) -> None:
        # Resolve the root once so containment checks compare real paths.
        self.workspace_root = Path(workspace_root).resolve()
        self.audit_log: list[ApprovalRecord] = []
        self._session_grants: set[str] = set()

    @property
    def session_grants(self) -> list[str]:
        """Active session-grant keys, sorted for display."""
        return sorted(self._session_grants)

    def evaluate(self, tool_name: str, arguments: dict) -> PolicyDecision:
        """Classify one tool call; session grants are consulted before asking."""
        decision = self._classify(tool_name, arguments)
        if (
            decision.decision is Decision.ASK
            and decision.session_grantable
            and decision.grant_key in self._session_grants
        ):
            return replace(
                decision,
                decision=Decision.ALLOW,
                reason=(
                    f"{decision.reason} "
                    f"(allowed by session grant {decision.grant_key!r})"
                ),
            )
        return decision

    def approve_session(self, decision: PolicyDecision) -> None:
        """Turn one ASK decision into a session grant.

        Non-grantable decisions (composite commands, denials) are ignored, so a
        one-off approval can never silently become a standing permission.
        """
        if decision.session_grantable and decision.grant_key is not None:
            self._session_grants.add(decision.grant_key)

    def record(self, tool_name: str, decision: PolicyDecision, outcome: str) -> None:
        """Append the final disposition of a tool call to the audit log."""
        self.audit_log.append(
            ApprovalRecord(
                tool=tool_name,
                target=decision.target,
                outcome=outcome,
                reason=decision.reason,
                timestamp=time.time(),
            )
        )

    def snapshot(self) -> dict:
        """Return grants and audit records for a durable session checkpoint."""
        return {
            "session_grants": self.session_grants,
            "audit_log": [asdict(record) for record in self.audit_log],
        }

    def restore(self, snapshot: dict) -> None:
        """Apply validated checkpoint state without replaying approvals."""
        self._session_grants = set(snapshot["session_grants"])
        self.audit_log = [ApprovalRecord(**record) for record in snapshot["audit_log"]]

    def _classify(self, tool_name: str, arguments: dict) -> PolicyDecision:
        if tool_name in _READ_TOOLS:
            return self._classify_read(tool_name, arguments)
        if tool_name in _WRITE_TOOLS:
            return self._classify_write(tool_name, arguments)
        if tool_name == _BASH_TOOL:
            return self._classify_bash(arguments)
        if tool_name.startswith(_MCP_TOOL_PREFIX):
            return self._classify_mcp(tool_name)
        if tool_name in _SKILL_TOOLS:
            return self._classify_skill(tool_name, arguments)
        # Default-deny: tools missing from the classification table are denied
        # until a policy is written for them.
        return PolicyDecision(
            decision=Decision.DENY,
            reason=f"tool {tool_name!r} is not in the permission classification table",
            target=tool_name,
            grant_key=None,
            session_grantable=False,
        )

    def _classify_read(self, tool_name: str, arguments: dict) -> PolicyDecision:
        resolved = self._resolve(str(arguments.get("path", ".")))
        if resolved.is_relative_to(self.workspace_root):
            return PolicyDecision(
                decision=Decision.ALLOW,
                reason=f"{tool_name} reads inside the workspace",
                target=str(resolved),
                grant_key=None,
                session_grantable=False,
            )
        # Reads outside the workspace ask instead of being denied: reading is
        # reversible and often legitimate (logs, system headers, examples).
        return PolicyDecision(
            decision=Decision.ASK,
            reason=f"{tool_name} reads outside the workspace",
            target=str(resolved),
            grant_key=_READ_OUTSIDE_GRANT,
            session_grantable=True,
        )

    def _classify_write(self, tool_name: str, arguments: dict) -> PolicyDecision:
        resolved = self._resolve(str(arguments.get("path", ".")))
        if not resolved.is_relative_to(self.workspace_root):
            return PolicyDecision(
                decision=Decision.DENY,
                reason=f"{tool_name} writes outside the workspace",
                target=str(resolved),
                grant_key=None,
                session_grantable=False,
            )
        return PolicyDecision(
            decision=Decision.ASK,
            reason=f"{tool_name} writes inside the workspace",
            target=str(resolved),
            grant_key=_WORKSPACE_WRITE_GRANT,
            session_grantable=True,
        )

    def _classify_mcp(self, tool_name: str) -> PolicyDecision:
        # MCP tools (M6) execute code supplied by an external server, so they
        # can never be silently allowed. They also cannot be classified by
        # path or command content, so the default is ASK, and a session grant
        # covers exactly this one server tool, keyed by its registered name.
        return PolicyDecision(
            decision=Decision.ASK,
            reason=f"MCP tool {tool_name!r} is provided by an external server",
            target=tool_name,
            grant_key=f"mcp:{tool_name}",
            session_grantable=True,
        )

    def _classify_skill(self, tool_name: str, arguments: dict) -> PolicyDecision:
        # Skill loads (M7) are reads the loader itself confines to indexed
        # skill directories, so there is no path or command left to classify.
        return PolicyDecision(
            decision=Decision.ALLOW,
            reason=f"{tool_name} reads inside indexed skill directories",
            target=str(arguments.get("name", tool_name)),
            grant_key=None,
            session_grantable=False,
        )

    def _classify_bash(self, arguments: dict) -> PolicyDecision:
        command = str(arguments.get("command", ""))
        normalized = " ".join(command.split())
        # High-risk patterns are matched against the full raw text so they also
        # fire inside composite commands (e.g. "ls && sudo ...").
        for pattern, description in _HIGH_RISK_PATTERNS:
            if pattern.search(command):
                return PolicyDecision(
                    decision=Decision.DENY,
                    reason=f"high-risk command ({description})",
                    target=normalized,
                    grant_key=None,
                    session_grantable=False,
                )
        if _has_composite_syntax(command):
            return PolicyDecision(
                decision=Decision.ASK,
                reason="composite shell command; approved per invocation only",
                target=normalized,
                grant_key=None,
                session_grantable=False,
            )
        return PolicyDecision(
            decision=Decision.ASK,
            reason="shell command",
            target=normalized,
            grant_key=f"bash:{normalized}",
            session_grantable=True,
        )

    def _resolve(self, raw_path: str) -> Path:
        """Resolve a model-supplied path against the workspace root.

        Relative paths are anchored at the workspace root (not the process
        cwd), and resolve() collapses ``..`` and follows symlinks, so escapes
        are judged by where the path actually ends up.
        """
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
        return candidate.resolve()
