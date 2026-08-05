"""Versioned, atomic checkpoints for resumable agent sessions.

Checkpoint files are intentionally plain JSON: they are inspectable, easy to
migrate, and do not execute code while loading.  This module owns the durable
schema boundary; runtime classes only expose JSON-compatible snapshots.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
from uuid import uuid4

from jsonschema import Draft202012Validator

from .context import ContextManager, InvalidMessageHistory
from .runtime import AgentStateMachine, InvalidStateTransition


CURRENT_SCHEMA_VERSION = 2
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SECRET_TEXT = re.compile(
    r"(?i)(?:\b(?:OPENAI|DEEPSEEK)_API_KEY\s*=\s*\S+|\bsk-[A-Za-z0-9_-]{8,})"
)
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
_TERMINAL_STATES = {"completed", "failed", "limit_reached", "interrupted"}
_ALL_STATES = {
    "idle",
    "requesting_model",
    "executing_tools",
    "waiting_approval",
    *_TERMINAL_STATES,
}


class CheckpointError(RuntimeError):
    """Base class for actionable checkpoint failures."""


class CheckpointNotFoundError(CheckpointError):
    pass


class CheckpointCorruptError(CheckpointError):
    pass


class UnsupportedCheckpointVersion(CheckpointError):
    pass


class WorkspaceMismatchError(CheckpointError):
    pass


class SensitiveCheckpointError(CheckpointError):
    pass


@dataclass(frozen=True)
class RestoredCheckpoint:
    """Validated current-version state ready to apply to an Agent."""

    session_id: str
    payload: dict[str, Any]
    migrated_from: int | None = None


_SCHEMA_V2: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "session_id",
        "saved_at",
        "workspace",
        "agent",
        "permissions",
        "tool_traces",
    ],
    "properties": {
        "schema_version": {"const": 2},
        "session_id": {"type": "string", "pattern": _SESSION_ID.pattern},
        "saved_at": {"type": "string", "format": "date-time"},
        "workspace": {
            "type": "object",
            "additionalProperties": False,
            "required": ["root", "identity"],
            "properties": {
                "root": {"type": "string", "minLength": 1},
                "identity": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        "agent": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "model",
                "messages",
                "task_summary",
                "runtime",
                "budget",
            ],
            "properties": {
                "model": {"type": "string", "minLength": 1},
                "messages": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["role"],
                        "properties": {
                            "role": {"enum": ["system", "user", "assistant", "tool"]},
                        },
                    },
                },
                "task_summary": {"type": ["string", "null"]},
                "runtime": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["state", "history"],
                    "properties": {
                        "state": {"enum": sorted(_TERMINAL_STATES)},
                        "history": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["previous", "current", "reason"],
                                "properties": {
                                    "previous": {"enum": sorted(_ALL_STATES)},
                                    "current": {"enum": sorted(_ALL_STATES)},
                                    "reason": {"type": "string"},
                                },
                            },
                        },
                    },
                },
                "budget": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "limits",
                        "last_prompt_tokens",
                        "total_prompt_tokens",
                        "total_completion_tokens",
                        "last_context_stats",
                    ],
                    "properties": {
                        "limits": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "max_input_tokens",
                                "max_output_tokens",
                                "instruction_tokens",
                                "task_state_tokens",
                                "recent_dialogue_tokens",
                                "tool_result_tokens",
                                "max_tool_result_tokens",
                            ],
                            "properties": {
                                name: {"type": "integer", "minimum": 1}
                                for name in (
                                    "max_input_tokens",
                                    "max_output_tokens",
                                    "instruction_tokens",
                                    "task_state_tokens",
                                    "recent_dialogue_tokens",
                                    "tool_result_tokens",
                                    "max_tool_result_tokens",
                                )
                            },
                        },
                        "last_prompt_tokens": {"type": ["integer", "null"], "minimum": 0},
                        "total_prompt_tokens": {"type": "integer", "minimum": 0},
                        "total_completion_tokens": {"type": "integer", "minimum": 0},
                        "last_context_stats": {
                            "anyOf": [
                                {"type": "null"},
                                {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": [
                                        "tokenizer",
                                        "estimated_input_tokens",
                                        "max_input_tokens",
                                        "instruction_tokens",
                                        "task_state_tokens",
                                        "recent_dialogue_tokens",
                                        "tool_result_tokens",
                                        "dropped_groups",
                                        "trimmed_tool_results",
                                    ],
                                    "properties": {
                                        "tokenizer": {"type": "string"},
                                        **{
                                            name: {"type": "integer", "minimum": 0}
                                            for name in (
                                                "estimated_input_tokens",
                                                "max_input_tokens",
                                                "instruction_tokens",
                                                "task_state_tokens",
                                                "recent_dialogue_tokens",
                                                "tool_result_tokens",
                                                "dropped_groups",
                                                "trimmed_tool_results",
                                            )
                                        },
                                    },
                                },
                            ]
                        },
                    },
                },
            },
        },
        "permissions": {
            "type": "object",
            "additionalProperties": False,
            "required": ["session_grants", "audit_log"],
            "properties": {
                "session_grants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
                "audit_log": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["tool", "target", "outcome", "reason", "timestamp"],
                        "properties": {
                            "tool": {"type": "string"},
                            "target": {"type": "string"},
                            "outcome": {"type": "string"},
                            "reason": {"type": "string"},
                            "timestamp": {"type": "number"},
                        },
                    },
                },
            },
        },
        "tool_traces": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "call_id",
                    "tool_name",
                    "arguments",
                    "ok",
                    "error_code",
                    "duration_ms",
                    "metadata",
                ],
                "properties": {
                    "call_id": {"type": ["string", "null"]},
                    "tool_name": {"type": "string"},
                    "arguments": {"type": "object"},
                    "ok": {"type": "boolean"},
                    "error_code": {"type": ["string", "null"]},
                    "duration_ms": {"type": "number", "minimum": 0},
                    "metadata": {"type": "object"},
                },
            },
        },
    },
}


class CheckpointStore:
    """Persist sessions below one workspace and restore them safely."""

    def __init__(self, workspace_root: Path, directory: Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.directory = Path(directory) if directory is not None else (
            self.workspace_root / ".sakicode" / "checkpoints"
        )

    @staticmethod
    def new_session_id() -> str:
        return uuid4().hex

    def save(self, agent: Any, session_id: str) -> Path:
        """Atomically replace one session checkpoint with the latest safe state."""
        self._validate_session_id(session_id)
        if agent.permission_engine.workspace_root != self.workspace_root:
            raise WorkspaceMismatchError(
                "agent permission root does not match the checkpoint workspace"
            )
        runtime = agent.runtime.snapshot()
        if runtime["state"] not in _TERMINAL_STATES:
            raise CheckpointError("checkpoints may only be saved in a terminal runtime state")

        payload = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "session_id": session_id,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "workspace": {
                "root": str(self.workspace_root),
                "identity": self._workspace_identity(self.workspace_root),
            },
            "agent": {
                "model": agent.model,
                "messages": _stable_messages(agent.messages),
                "task_summary": agent.task_summary,
                "runtime": runtime,
                "budget": {
                    "limits": asdict(agent.context_manager.budget),
                    "last_prompt_tokens": agent.last_prompt_tokens,
                    "total_prompt_tokens": agent.total_prompt_tokens,
                    "total_completion_tokens": agent.total_completion_tokens,
                    "last_context_stats": (
                        asdict(agent.last_context_stats)
                        if agent.last_context_stats is not None
                        else None
                    ),
                },
            },
            "permissions": agent.permission_engine.snapshot(),
            "tool_traces": [trace.to_dict() for trace in agent.tool_registry.traces],
        }
        payload = _redact_secrets(payload)
        self._validate_current(payload)
        self._validate_semantics(payload)
        encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        target = self.path_for(session_id)
        temporary: Path | None = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{session_id}.", suffix=".tmp", dir=self.directory
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
            # Persist the directory entry itself where the platform supports it.
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError as error:
            raise CheckpointError(f"could not atomically save checkpoint: {error}") from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    def load(self, session_id: str) -> RestoredCheckpoint:
        """Load, migrate, validate, and workspace-bind a checkpoint."""
        path = self.path_for(session_id)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as error:
            raise CheckpointNotFoundError(f"checkpoint {session_id!r} was not found") from error
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CheckpointCorruptError(f"checkpoint {session_id!r} is not valid JSON") from error
        if not isinstance(payload, dict):
            raise CheckpointCorruptError("checkpoint root must be a JSON object")
        if _contains_secret(payload):
            raise SensitiveCheckpointError("checkpoint contains an unredacted sensitive value")

        original_version = payload.get("schema_version")
        if not isinstance(original_version, int):
            raise CheckpointCorruptError("checkpoint schema_version must be an integer")
        migrated_from = None
        if original_version != CURRENT_SCHEMA_VERSION:
            payload = self._migrate(payload)
            migrated_from = original_version
        self._validate_current(payload)
        self._validate_semantics(payload)
        if payload["session_id"] != session_id:
            raise CheckpointCorruptError("checkpoint session_id does not match its filename")
        expected = self._workspace_identity(self.workspace_root)
        if (
            payload["workspace"]["identity"] != expected
            or payload["workspace"]["root"] != str(self.workspace_root)
        ):
            raise WorkspaceMismatchError(
                f"checkpoint belongs to {payload['workspace']['root']!r}, "
                f"not {str(self.workspace_root)!r}"
            )
        return RestoredCheckpoint(session_id, payload, migrated_from)

    def path_for(self, session_id: str) -> Path:
        self._validate_session_id(session_id)
        return self.directory / f"{session_id}.json"

    @staticmethod
    def _workspace_identity(root: Path) -> str:
        return hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_session_id(session_id: str) -> None:
        if not _SESSION_ID.fullmatch(session_id):
            raise ValueError("session id must contain only letters, digits, '_' or '-'")

    @staticmethod
    def _validate_current(payload: Mapping[str, Any]) -> None:
        errors = sorted(
            Draft202012Validator(_SCHEMA_V2).iter_errors(payload),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            error = errors[0]
            location = ".".join(str(part) for part in error.absolute_path) or "root"
            raise CheckpointCorruptError(f"invalid checkpoint at {location}: {error.message}")

    @staticmethod
    def _validate_semantics(payload: Mapping[str, Any]) -> None:
        try:
            AgentStateMachine.from_snapshot(payload["agent"]["runtime"])
            ContextManager.validate_tool_pairs(payload["agent"]["messages"])
        except (InvalidStateTransition, InvalidMessageHistory, ValueError) as error:
            raise CheckpointCorruptError(
                f"checkpoint violates runtime/message invariants: {error}"
            ) from error

    def _migrate(self, payload: dict[str, Any]) -> dict[str, Any]:
        version = payload.get("schema_version")
        if version != 1:
            raise UnsupportedCheckpointVersion(
                f"checkpoint schema {version!r} is unsupported; "
                f"current schema is {CURRENT_SCHEMA_VERSION}"
            )
        # V1 used flat names.  Migration is pure and leaves the source file
        # untouched; the next successful save writes V2 atomically.
        root = str(Path(payload.get("workspace_root", "")).resolve())
        return {
            "schema_version": 2,
            "session_id": payload.get("session_id"),
            "saved_at": payload.get("saved_at"),
            "workspace": {
                "root": root,
                "identity": self._workspace_identity(Path(root)),
            },
            "agent": {
                "model": payload.get("model"),
                "messages": payload.get("messages"),
                "task_summary": payload.get("task_summary"),
                "runtime": payload.get("runtime"),
                "budget": payload.get("budget_usage"),
            },
            "permissions": {
                "session_grants": payload.get("session_grants", []),
                "audit_log": payload.get("approval_audit", []),
            },
            "tool_traces": payload.get("tool_traces", []),
        }


def _stable_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the longest prefix without a half-written tool-call bundle."""
    stable: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        calls = message.get("tool_calls") if message.get("role") == "assistant" else None
        if not calls:
            if message.get("role") == "tool":
                break
            stable.append(message)
            index += 1
            continue
        expected = [call.get("id") for call in calls]
        end = index + 1 + len(expected)
        results = messages[index + 1 : end]
        valid = len(results) == len(expected) and all(
            result.get("role") == "tool" and result.get("tool_call_id") == call_id
            for result, call_id in zip(results, expected)
        )
        if not valid:
            break
        stable.extend(messages[index:end])
        index = end
    return stable


def _redact_secrets(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {item_key: _redact_secrets(item, item_key) for item_key, item in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _SECRET_TEXT.sub("[REDACTED]", value)
    return value


def _contains_secret(value: Any, key: str | None = None) -> bool:
    if key is not None and _is_sensitive_key(key) and value != "[REDACTED]":
        return True
    if isinstance(value, dict):
        return any(_contains_secret(item, item_key) for item_key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return isinstance(value, str) and _SECRET_TEXT.search(value) is not None


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )
