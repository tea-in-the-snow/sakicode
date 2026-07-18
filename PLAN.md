# sakicode — v1 design contract

An AI coding-agent CLI in the spirit of Claude Code / Kimi CLI, built as a
**learning project**: implementation clarity beats feature count.

## Decisions

- **Language**: Python 3.10+, full project scaffolding (`src/` layout,
  `pyproject.toml` with a `sakicode` entry point, pytest).
- **Model layer**: `openai` SDK against any OpenAI-compatible endpoint.
  Developed against DeepSeek (`https://api.deepseek.com`).
- **Config**: env vars `OPENAI_BASE_URL` / `OPENAI_API_KEY`
  (`DEEPSEEK_API_KEY` accepted as fallback), overridable with
  `--base-url` / `--model` CLI flags. A `./.env` file in the current
  directory is loaded automatically (lowest precedence, never overriding
  real env vars). Default model: `deepseek-chat`.
- **Interaction**: simple REPL; responses are **streamed** and rendered with
  `rich`. Tool-call deltas are accumulated during streaming.
- **Tools (core 6)**: `read_file`, `write_file`, `edit_file`, `run_bash`,
  `glob`, `grep`.
- **Permissions**: `read_file` / `glob` / `grep` run freely;
  `write_file` / `edit_file` / `run_bash` require an interactive y/n
  confirmation.
- **Agent loop guardrails**: max ~25 tool calls per user turn, then break with
  a message; tool errors are fed back to the model as tool results so it can
  self-correct; API errors abort the turn visibly.
- **Context**: full history every turn; rough token estimate warns near the
  context limit. System prompt = base prompt + auto-loaded `./AGENTS.md`.
- **Definition of done**: sakicode, pointed at its own repo, completes a real
  task end-to-end (e.g. "add a `--version` flag") with human approvals —
  plus passing pytest coverage for the tools.

## Explicitly deferred (post-v1)

Context compaction/summarization, session persistence, full TUI
(prompt_toolkit/textual), permission rules engine, retry/backoff,
plugins/MCP, multi-provider abstraction.
