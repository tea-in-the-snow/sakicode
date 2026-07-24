# sakicode

A minimal AI coding-agent CLI (learning project), built against any
OpenAI-compatible endpoint (DeepSeek by default). See PLAN.md for the v1
design contract.

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

Options: `--model` (default `deepseek-chat`), `--base-url` (default
`https://api.deepseek.com`, also configurable via `OPENAI_BASE_URL`).

Inside the REPL, type your request at the `saki> ` prompt. Use `/runtime` to
inspect state transitions and `/trace` to inspect structured tool calls;
`exit`, `quit`, Ctrl-C or Ctrl-D leaves. Sensitive tool arguments are redacted
from traces. `write_file`, `edit_file` and `run_bash` ask for confirmation
before running.

Run the tests with:

```bash
uv run pytest
```
