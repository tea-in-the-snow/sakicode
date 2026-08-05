"""M8: end-to-end evaluation harness — fixed task set, metrics, and replay.

Each task under ``evals/tasks/<name>/`` pairs a fixture ``workspace/`` with a
``task.json`` (prompt, approval policy, declarative grading checks). A run
copies the fixture into a scratch directory, drives one agent turn against a
real or scripted model client, grades the outcome, and records the metrics the
resume claims depend on: success, tool calls, tokens, wall time, approvals,
and checkpoint-recovery success. Results are JSON files, so runs are
reproducible and two runs can be diffed with ``--compare``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from openai import OpenAI
from rich.console import Console

from . import tools
from .agent import Agent
from .checkpoint import CheckpointError, CheckpointStore
from .config import load_config
from .permissions import PermissionEngine
from .prompts import build_system_prompt
from .runtime import AgentState
from .sandbox import SandboxPolicy, bwrap_available

GRADE_COMMAND_TIMEOUT = 120  # seconds
_ALLOWED_OUTCOMES = {"allow_once", "allow_session", "session_grant_hit", "policy_allow"}


@dataclass(frozen=True)
class EvalTask:
    """One fixed evaluation task loaded from a task directory."""

    name: str
    prompt: str
    approval: str  # "allow" | "deny": how ASK decisions are answered
    checks: list[dict]
    directory: Path
    sandbox: str = "bwrap"  # "bwrap" | "off": sandbox approved bash commands
    network: bool = False  # whether the sandbox may reach the network
    requires: str | None = None  # e.g. "bwrap": skip when unavailable


@dataclass
class CheckResult:
    """The graded outcome of one declarative check."""

    type: str
    passed: bool
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TaskResult:
    """Metrics and grading evidence for one task run."""

    name: str
    success: bool
    final_state: str
    checks: list[CheckResult]
    tool_calls: int
    tool_breakdown: dict[str, int]
    prompt_tokens: int
    completion_tokens: int
    duration_s: float
    approvals: dict[str, int]
    recovery_ok: bool
    session_id: str | None
    skipped: bool = False
    transcript: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["checks"] = [check.to_dict() for check in self.checks]
        return data


def load_tasks(tasks_dir: Path, only: str | None = None) -> list[EvalTask]:
    """Load every task directory holding a task.json, sorted by name."""
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise EvalError(f"tasks directory {tasks_dir} does not exist")
    tasks = []
    for task_dir in sorted(path for path in tasks_dir.iterdir() if path.is_dir()):
        spec_path = task_dir / "task.json"
        if not spec_path.is_file():
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        name = spec.get("name", task_dir.name)
        if only is not None and name != only:
            continue
        if not (task_dir / "workspace").is_dir():
            raise EvalError(f"task {name!r} has no workspace/ fixture directory")
        approval = spec.get("approval", "allow")
        if approval not in ("allow", "deny"):
            raise EvalError(f"task {name!r}: approval must be 'allow' or 'deny'")
        sandbox = spec.get("sandbox", "bwrap")
        if sandbox not in ("bwrap", "off"):
            raise EvalError(f"task {name!r}: sandbox must be 'bwrap' or 'off'")
        tasks.append(
            EvalTask(
                name=name,
                prompt=spec["prompt"],
                approval=approval,
                checks=list(spec.get("checks", [])),
                directory=task_dir,
                sandbox=sandbox,
                network=bool(spec.get("network", False)),
                requires=spec.get("requires"),
            )
        )
    if only is not None and not tasks:
        raise EvalError(f"no task named {only!r} under {tasks_dir}")
    return tasks


class EvalError(Exception):
    """Configuration or fixture problem, distinct from a failed task."""


def run_task(task: EvalTask, client, model: str, work_root: Path) -> TaskResult:
    """Run one task in an isolated copy of its fixture workspace."""
    if task.requires == "bwrap" and not bwrap_available():
        return TaskResult(
            name=task.name,
            success=False,
            final_state="skipped",
            checks=[],
            tool_calls=0,
            tool_breakdown={},
            prompt_tokens=0,
            completion_tokens=0,
            duration_s=0.0,
            approvals={},
            recovery_ok=False,
            session_id=None,
            skipped=True,
        )
    workdir = Path(work_root) / task.name
    if workdir.exists():
        shutil.rmtree(workdir)
    shutil.copytree(task.directory / "workspace", workdir)

    sandbox_policy = None
    if task.sandbox == "bwrap" and bwrap_available():
        sandbox_policy = SandboxPolicy(network=task.network)
    console = Console(file=io.StringIO())
    previous_cwd = Path.cwd()
    os.chdir(workdir)
    try:
        agent = Agent(
            client=client,
            model=model,
            system_prompt=build_system_prompt(),
            console=console,
            tool_registry=tools.create_registry(
                sandbox_policy=sandbox_policy, workspace=workdir
            ),
            permission_engine=PermissionEngine(Path.cwd()),
            checkpoint_store=CheckpointStore(Path.cwd()),
            approval_handler=_approval_handler(task.approval),
        )
        started = time.perf_counter()
        agent.run_turn(task.prompt)
        duration_s = time.perf_counter() - started
    finally:
        os.chdir(previous_cwd)

    checks = [_run_check(check, workdir, agent) for check in task.checks]
    final_state = agent.runtime.state.value
    success = all(check.passed for check in checks) and (
        agent.runtime.state is AgentState.COMPLETED
    )
    recovery_ok = _verify_recovery(agent)
    approvals: dict[str, int] = {}
    for record in agent.permission_engine.audit_log:
        approvals[record.outcome] = approvals.get(record.outcome, 0) + 1
    breakdown: dict[str, int] = {}
    for trace in agent.tool_registry.traces:
        breakdown[trace.tool_name] = breakdown.get(trace.tool_name, 0) + 1
    transcript = {
        "messages": agent.messages,
        "tool_traces": [trace.to_dict() for trace in agent.tool_registry.traces],
        "audit_log": [asdict(r) for r in agent.permission_engine.audit_log],
        "console_output": console.file.getvalue(),
    }
    return TaskResult(
        name=task.name,
        success=success,
        final_state=final_state,
        checks=checks,
        tool_calls=len(agent.tool_registry.traces),
        tool_breakdown=breakdown,
        prompt_tokens=agent.total_prompt_tokens,
        completion_tokens=agent.total_completion_tokens,
        duration_s=round(duration_s, 3),
        approvals=approvals,
        recovery_ok=recovery_ok,
        session_id=agent.session_id,
        transcript=transcript,
    )


def _approval_handler(policy_name: str):
    """Answer every ASK with the task's configured approval policy."""

    def handler(_policy) -> str:
        return "allow_once" if policy_name == "allow" else "deny"

    return handler


def _verify_recovery(agent: Agent) -> bool:
    """Reload the saved checkpoint and confirm it restores the full history."""
    if agent.checkpoint_store is None or agent.session_id is None:
        return False
    try:
        restored = agent.checkpoint_store.load(agent.session_id)
    except (CheckpointError, ValueError):
        return False
    restored_messages = restored.payload["agent"]["messages"]
    return len(restored_messages) == len(agent.messages)


def _run_check(check: dict, workdir: Path, agent: Agent) -> CheckResult:
    check_type = check.get("type")
    try:
        if check_type == "command":
            return _check_command(check, workdir)
        if check_type == "file_contains":
            return _check_file_contains(check, workdir, expect=True)
        if check_type == "file_not_contains":
            return _check_file_contains(check, workdir, expect=False)
        if check_type == "file_exists":
            return _check_file_exists(check, workdir)
        if check_type == "audit_outcome":
            return _check_audit_outcome(check, agent)
        if check_type == "command_not_executed":
            return _check_command_not_executed(check, agent)
    except OSError as error:
        return CheckResult(str(check_type), False, f"check error: {error}")
    return CheckResult(str(check_type), False, f"unknown check type {check_type!r}")


def _check_command(check: dict, workdir: Path) -> CheckResult:
    expect_exit = check.get("expect_exit", 0)
    try:
        process = subprocess.run(
            ["bash", "-c", check["run"]],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=GRADE_COMMAND_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            "command", False, f"grading command timed out: {check['run']!r}"
        )
    passed = process.returncode == expect_exit
    detail = f"{check['run']!r} exited {process.returncode} (expected {expect_exit})"
    return CheckResult("command", passed, detail)


def _check_file_contains(check: dict, workdir: Path, expect: bool) -> CheckResult:
    path = workdir / check["path"]
    check_type = "file_contains" if expect else "file_not_contains"
    if not path.is_file():
        if expect:
            return CheckResult(check_type, False, f"{check['path']} does not exist")
        return CheckResult(check_type, True, f"{check['path']} does not exist")
    found = re.search(check["pattern"], path.read_text(encoding="utf-8")) is not None
    passed = found if expect else not found
    detail = f"pattern {check['pattern']!r} {'found' if found else 'absent'} in {check['path']}"
    return CheckResult(check_type, passed, detail)


def _check_file_exists(check: dict, workdir: Path) -> CheckResult:
    exists = (workdir / check["path"]).exists()
    return CheckResult(
        "file_exists",
        exists,
        f"{check['path']} {'exists' if exists else 'is missing'}",
    )


def _check_audit_outcome(check: dict, agent: Agent) -> CheckResult:
    matched = any(
        record.tool == check["tool"] and record.outcome == check["outcome"]
        for record in agent.permission_engine.audit_log
    )
    detail = (
        f"audit log has {check['tool']}/{check['outcome']}"
        if matched
        else f"no {check['tool']}/{check['outcome']} record in audit log"
    )
    return CheckResult("audit_outcome", matched, detail)


def _check_command_not_executed(check: dict, agent: Agent) -> CheckResult:
    """Guardrail invariant: no approved bash call matched the danger pattern.

    Trace arguments for run_bash are redacted (x-sensitive), so the audit
    log's normalized targets are the evidence: any *allowed* run_bash record
    whose target matches the pattern means the command really ran.
    """
    pattern = re.compile(check["pattern"])
    for record in agent.permission_engine.audit_log:
        if (
            record.tool == "run_bash"
            and record.outcome in _ALLOWED_OUTCOMES
            and pattern.search(record.target)
        ):
            return CheckResult(
                "command_not_executed",
                False,
                f"dangerous command was approved and ran: {record.target!r}",
            )
    return CheckResult(
        "command_not_executed", True, "no allowed command matched the pattern"
    )


def run_suite(tasks: list[EvalTask], client, model: str, out_dir: Path) -> dict:
    """Run all tasks, persist results and transcripts, return the report."""
    out_dir = Path(out_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    work_root = out_dir / "workspaces" / run_id
    work_root.mkdir(parents=True, exist_ok=True)
    transcript_dir = out_dir / "transcripts" / run_id
    transcript_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for task in tasks:
        result = run_task(task, client, model, work_root)
        results.append(result)
        (transcript_dir / f"{task.name}.json").write_text(
            json.dumps(result.transcript, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    report = {
        "run_id": run_id,
        "model": model,
        "tasks": [result.to_dict() for result in results],
        "aggregate": _aggregate(results),
    }
    report_path = out_dir / f"{run_id}.json"
    # Transcripts live in their own per-task files; keep the report free of
    # bulk message history.
    for task_dict in report["tasks"]:
        task_dict.pop("transcript", None)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _aggregate(results: list[TaskResult]) -> dict:
    active = [r for r in results if not r.skipped]
    total = len(active)
    return {
        "tasks": total,
        "skipped": len(results) - total,
        "succeeded": sum(1 for r in active if r.success),
        "success_rate": (sum(1 for r in active if r.success) / total) if total else 0.0,
        "recovery_rate": (
            sum(1 for r in active if r.recovery_ok) / total if total else 0.0
        ),
        "total_prompt_tokens": sum(r.prompt_tokens for r in active),
        "total_completion_tokens": sum(r.completion_tokens for r in active),
        "total_tool_calls": sum(r.tool_calls for r in active),
        "total_duration_s": round(sum(r.duration_s for r in active), 3),
    }


def compare(report_a: dict, report_b: dict) -> dict:
    """Diff two run reports task-by-task and in aggregate."""
    tasks_a = {task["name"]: task for task in report_a["tasks"]}
    tasks_b = {task["name"]: task for task in report_b["tasks"]}
    rows = []
    for name in sorted(set(tasks_a) | set(tasks_b)):
        a, b = tasks_a.get(name), tasks_b.get(name)
        if a is None or b is None:
            rows.append({"task": name, "only_in": "A" if b is None else "B"})
            continue
        rows.append(
            {
                "task": name,
                "success": f"{'pass' if a['success'] else 'fail'} -> "
                f"{'pass' if b['success'] else 'fail'}",
                "d_prompt_tokens": b["prompt_tokens"] - a["prompt_tokens"],
                "d_completion_tokens": b["completion_tokens"] - a["completion_tokens"],
                "d_tool_calls": b["tool_calls"] - a["tool_calls"],
                "d_duration_s": round(b["duration_s"] - a["duration_s"], 3),
            }
        )
    agg_a, agg_b = report_a["aggregate"], report_b["aggregate"]
    return {
        "run_a": report_a["run_id"],
        "run_b": report_b["run_id"],
        "success_rate": f"{agg_a['success_rate']:.0%} -> {agg_b['success_rate']:.0%}",
        "recovery_rate": f"{agg_a['recovery_rate']:.0%} -> {agg_b['recovery_rate']:.0%}",
        "d_total_prompt_tokens": (
            agg_b["total_prompt_tokens"] - agg_a["total_prompt_tokens"]
        ),
        "d_total_duration_s": round(
            agg_b["total_duration_s"] - agg_a["total_duration_s"], 3
        ),
        "tasks": rows,
    }


def _print_report(console: Console, report: dict) -> None:
    agg = report["aggregate"]
    console.print(f"[bold]Run {report['run_id']}[/bold] (model: {report['model']})")
    for task in report["tasks"]:
        if task.get("skipped"):
            console.print(f"  [yellow]SKIP[/yellow] {task['name']}: requirement missing")
            continue
        mark = "[green]PASS[/green]" if task["success"] else "[red]FAIL[/red]"
        console.print(
            f"  {mark} {task['name']}: {task['tool_calls']} tool calls, "
            f"{task['prompt_tokens']}+{task['completion_tokens']} tokens, "
            f"{task['duration_s']:.1f}s, recovery={'ok' if task['recovery_ok'] else 'FAILED'}"
        )
        for check in task["checks"]:
            if not check["passed"]:
                console.print(f"       [red]✗ {check['type']}: {check['detail']}[/red]")
    console.print(
        f"[bold]Success {agg['succeeded']}/{agg['tasks']} "
        f"({agg['success_rate']:.0%}), recovery {agg['recovery_rate']:.0%}, "
        f"skipped {agg['skipped']}, "
        f"tokens {agg['total_prompt_tokens']}+{agg['total_completion_tokens']}, "
        f"{agg['total_duration_s']:.1f}s[/bold]"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="sakicode-eval",
        description="Run the fixed evaluation task set and record metrics.",
    )
    parser.add_argument("--tasks", default="evals/tasks", help="task set directory")
    parser.add_argument("--out", default="evals/results", help="results directory")
    parser.add_argument("--task", help="run only the named task")
    parser.add_argument("--model", help="model name (default: deepseek-chat)")
    parser.add_argument("--base-url", help="OpenAI-compatible API base URL")
    parser.add_argument(
        "--compare",
        nargs=2,
        metavar=("RUN_A", "RUN_B"),
        help="diff two result JSON files and exit",
    )
    args = parser.parse_args()

    console = Console()
    if args.compare:
        report_a = json.loads(Path(args.compare[0]).read_text(encoding="utf-8"))
        report_b = json.loads(Path(args.compare[1]).read_text(encoding="utf-8"))
        console.print_json(json.dumps(compare(report_a, report_b)))
        return

    try:
        tasks = load_tasks(Path(args.tasks), only=args.task)
    except EvalError as error:
        console.print(f"[red]Eval config error: {error}[/red]")
        sys.exit(2)

    config = load_config(model=args.model, base_url=args.base_url)
    if not config.api_key:
        console.print(
            "[red]Error: no API key found.[/red] Evaluations run against a real "
            "model; set OPENAI_API_KEY (or DEEPSEEK_API_KEY) and try again."
        )
        sys.exit(1)
    client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    report = run_suite(tasks, client, config.model, Path(args.out))
    _print_report(console, report)
    console.print(f"[dim]Report written to {args.out}/{report['run_id']}.json[/dim]")
    if not all(
        task["success"] for task in report["tasks"] if not task.get("skipped")
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
