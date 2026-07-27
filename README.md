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
`https://api.deepseek.com`, also configurable via `OPENAI_BASE_URL`).

Inside the REPL, type your request at the `saki> ` prompt. Use `/runtime` to
inspect state transitions, `/trace` to inspect structured tool calls,
`/approvals` to inspect permission decisions and active session grants, and
`/context` to inspect the four token-budget layers and compaction statistics;
`exit`, `quit`, Ctrl-C or Ctrl-D leaves. Sensitive tool arguments are redacted
from traces. A permission engine classifies every tool call as allow/ask/deny:
writes outside the workspace and high-risk commands are denied outright, other
writes and shell commands ask (with "once" and "this kind for the session"
grants), and read-only calls inside the workspace run directly.

Before each model request, context is assembled from immutable instructions,
a structured task-state summary, recent dialogue, and bounded tool results.
Known OpenAI models use their `tiktoken` encoding; unknown/provider-specific
models use a deliberately conservative UTF-8 byte estimate. Old tool-call
bundles are compacted atomically, so assistant calls never lose their matching
tool results. See
[the M4 learning notes](docs/learning/04-layered-context-and-token-budget.md).

Run the tests with:

```bash
uv run pytest
```
