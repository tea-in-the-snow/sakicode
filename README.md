# sakicode

A minimal AI coding-agent CLI (learning project), built against any
OpenAI-compatible endpoint (DeepSeek by default). 

The project is now being evolved from that v1 baseline into an extensible
coding-agent runtime. See [ROADMAP.md](ROADMAP.md) for the resume-aligned
implementation milestones, learning topics, and acceptance criteria.

## Usage

```bash
uv sync --extra dev    # or: python3 -m venv .venv && pip install -e .[dev]

export OPENAI_API_KEY=sk-...   # or DEEPSEEK_API_KEY
uv run sakicode                # start the REPL
```

Instead of exporting, you can put the key in a `.env` file in the directory
you run `sakicode` from (loaded automatically; real env vars take precedence,
and `.env` is git-ignored so the key stays out of commits):

```bash
echo 'OPENAI_API_KEY=sk-...' > .env
```

Options: `--model` (default `deepseek-v4-flash`), `--base-url` (default
`https://api.deepseek.com`, also configurable via `OPENAI_BASE_URL`), and
`--resume SESSION_ID` for a checkpointed session.

Inside the REPL, type your request at the `saki> ` prompt. Use `/runtime` to
inspect state transitions, `/trace` to inspect structured tool calls,
`/approvals` to inspect permission decisions and active session grants, and
`/context` to inspect the four token-budget layers and compaction statistics;
`exit`, `quit`, Ctrl-C or Ctrl-D leaves. Sensitive tool arguments are redacted
from traces. A permission engine classifies every tool call as allow/ask/deny:
writes outside the workspace and high-risk commands are denied outright, other
writes and shell commands ask (with "once" and "this kind for the session"
grants), and read-only calls inside the workspace run directly.

Approved shell commands additionally run inside a bubblewrap sandbox (Linux):
the filesystem is read-only outside the workspace, `/tmp` is private, the
network is off, credential directories like `~/.ssh` are masked, and
secret-looking environment variables (API keys, tokens) never enter the
sandbox. Control it with `--sandbox auto|bwrap|off` (default `auto`, or
`SAKICODE_SANDBOX`); when bwrap is missing, `auto` warns and falls back to the
permission engine alone. See
[the M9 learning notes](docs/learning/09-sandbox.md).

Before each model request, context is assembled from immutable instructions,
a structured task-state summary, recent dialogue, and bounded tool results.
Known OpenAI models use their `tiktoken` encoding; unknown/provider-specific
models use a deliberately conservative UTF-8 byte estimate. Old tool-call
bundles are compacted atomically, so assistant calls never lose their matching
tool results. See
[the M4 learning notes](docs/learning/04-layered-context-and-token-budget.md).

Every terminal turn is saved as a versioned, workspace-bound checkpoint under
`.sakicode/checkpoints/`. The CLI prints the session id at startup; after
leaving and restarting the process, resume it with:

```bash
uv run sakicode --resume <session-id>
```

Checkpoint replacement is atomic, older schemas are migrated while loading,
and messages, token usage, runtime history, permission grants/audit records,
and redacted tool traces are restored without replaying tool side effects. See
[the M5 learning notes](docs/learning/05-checkpoint-and-recovery.md).

External tools can be supplied by MCP servers: list them in
`.sakicode/mcp.json` (or pass `--mcp-config PATH`) and each server is spawned
as a subprocess speaking newline-delimited JSON-RPC over stdio. Discovered
tools are registered as `mcp__<server>__<tool>` through the same registry,
schema validation, tracing, and permission engine as built-ins (MCP tools ask
by default, grantable per tool for the session). Every request has a hard
timeout; on timeout, protocol garbage, or a crash the subprocess is killed
and the client fails fast instead of dragging the agent down. See
[the M6 learning notes](docs/learning/06-mcp-client.md).

Skills are declarative instruction packs: a directory with a `SKILL.md`
(name/description frontmatter plus a Markdown body) and optional bundled
resources, discovered from builtin, user (`~/.sakicode/skills/`) and project
(`.sakicode/skills/`) scopes — higher scopes shadow lower ones, with every
override or rejected file reported as a diagnostic. Startup parses
frontmatter only, so the system prompt carries just a name/description
index; the model loads a body or resource on demand through the `use_skill`
tool, which goes through the same registry and permission engine and
confines every read to the skill's own directory. Use `/skills` in the REPL
to inspect the index and diagnostics. See
[the M7 learning notes](docs/learning/07-skill-system.md).

## Evaluation harness

A fixed task set under `evals/tasks/` (add a CLI flag, fix a failing test, a
cross-file refactor, refuse a dangerous command, escape the sandbox) replays
the agent end to end.
Each task pairs a fixture `workspace/` with a `task.json` (prompt, approval
policy, declarative grading checks). Running

```bash
uv run sakicode-eval                # needs a real API key
uv run sakicode-eval --task fix-failing-test
uv run sakicode-eval --compare evals/results/A.json evals/results/B.json
```

copies each fixture into a scratch directory, drives one agent turn, grades the
declarative checks, and records success, tool-call counts, token usage, wall
time, approval counts, and checkpoint-recovery success into
`evals/results/<run-id>.json` (full transcripts alongside). `--compare` diffs
two runs task by task. See
[the M8 learning notes](docs/learning/08-evaluation-harness.md).

Run the tests with:

```bash
uv run pytest
```
