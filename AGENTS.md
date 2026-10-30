# Metalclaw contributor guide

## Orientation

Metalclaw is a single-user, headless Python chatbot: Telegram, Discord, tool
calling, persistent SQLite conversations, vault memory, and a heartbeat scheduler.
There is no interactive bot CLI or HTTP server.

Read `CLAUDE.md` and `ai-docs/INDEX.md` first, then the topic-specific architecture
notes. Follow `ai-docs/conventions.md`, `ai-docs/testing.md`, and
`ai-docs/gotchas.md`. Update the matching architecture notes when behavior changes.

## Environment and commands

Python 3.14 is pinned by `.python-version`; use `uv` for project commands.
Dependencies and development tools are locked in `uv.lock`.

```bash
uv sync --locked
task build-ci          # non-mutating CI gates: Ruff, gitleaks, mypy, tests, imports
task test              # full suite, output suppressed on success
uv run pytest tests/test_routing.py -q
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run python -c "import bot, tools, registry, self_change, memory, config, mcp_client"
uv run python bot.py --help
task run               # uv run python bot.py; real credentials required
```

`task lint` **modifies files** with Ruff autofix/format. `task build` runs that
mutating lint, tests, then imports; it is not just an import check. This project
is run from source, not built into a wheel. CI requires `rg`, Task 3.x, and
gitleaks (the workflow pins 8.30.1).

Copy `config.example.yaml` to ignored `config.yaml` and configure a vault, provider,
and at least one of `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` before running.
Use `--no-telegram` or `--no-discord` to skip a frontend.
Do not start a live bot as a test; it can contact accounts and run scheduled work.

## Code conventions

- Keep the existing flat module layout. Use absolute imports except existing
  package re-exports; preserve lazy imports that prevent cycles.
- Keep canonical `messages.py` dataclasses in sessions. Provider wire formats
  belong only in provider adapters.
- Tools are synchronous and registered with `registry.tool`; re-export new tools
  from `tools/__init__.py`. Shared slash-command wiring belongs in
  `frontends/common.py`, not duplicated per frontend.
- Keep blocking provider, filesystem-lock, and SQLite operations off the async
  event loop. Load sessions per turn under the chat lock; commit only on success.
- Preserve append-only history. Never delete `history.db` rows to reset a session;
  use boundaries/status metadata. Keep new runtime record state in SQLite.
- Preserve atomic memory writes and locking. Never read `get_config()` at import
  time; tests must reset its cache after changing environment/config.
- Use module loggers and lazy `%` formatting. Never log credentials or auth headers.
- Add regression tests in `tests/`, using temporary vaults, isolated XDG paths,
  and mocked providers/network clients. Do not depend on real accounts.
- Keep changes focused; do not invent abstractions for hypothetical integrations.

## Secrets and runtime data

Never commit `.env`, `config.yaml`, tokens, private vault contents, databases,
agent sessions, or virtual environments. Configuration lookup is
`METALCLAW_CONFIG`, then local `config.yaml`, then
`~/.config/metalclaw/config.yaml`. Data defaults to
`~/.local/share/metalclaw/history.db`; logs to
`~/.local/state/metalclaw/metalclaw.log`.

The bot's `/add-tool` self-modification path requires the separate `claude` CLI;
Pi and Codex do not replace it. Disable `allow_self_modification` unless explicitly
needed and safe. See `DEVBOX.md` for this VM's workspace and tooling.
