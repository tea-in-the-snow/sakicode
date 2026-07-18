# sakicode

A minimal AI coding-agent CLI (learning project), built against any
OpenAI-compatible endpoint (DeepSeek by default). See PLAN.md for the v1
design contract.

## Usage

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[dev]

export OPENAI_API_KEY=sk-...   # or DEEPSEEK_API_KEY
sakicode                       # start the REPL
```

Instead of exporting, you can put the key in a `.env` file in the directory
you run `sakicode` from (loaded automatically; real env vars take precedence,
and `.env` is git-ignored so the key stays out of commits):

```bash
echo 'OPENAI_API_KEY=sk-...' > .env
```

Options: `--model` (default `deepseek-chat`), `--base-url` (default
`https://api.deepseek.com`, also configurable via `OPENAI_BASE_URL`).

Inside the REPL, type your request at the `saki> ` prompt; `exit`, `quit`,
Ctrl-C or Ctrl-D leaves. `write_file`, `edit_file` and `run_bash` ask for
confirmation before running.

Run the tests with:

```bash
pytest
```
