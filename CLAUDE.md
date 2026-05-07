# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Metalclaw is a chatbot that talks to a local Ollama model (`gemma4:latest` by default) with tool calling, exposed via a single executable (`bot.py`) that runs the CLI REPL, the Telegram frontend, and the Discord frontend together. It also has a controlled self-modification path that shells out to `claude -p` to edit its own source.

## Commands

```
task build        # Import-check all modules
task lint         # ruff check . + optional gitleaks scan
task test         # pytest -q
task daemon       # uv run python bot.py --daemon  (Telegram + Discord + heartbeat, no REPL)
uv run python bot.py                # CLI REPL + Telegram + Discord (each starts only if token is set)
uv run python bot.py --no-telegram  # skip Telegram
uv run python bot.py --no-discord   # skip Discord
```

Run a single test: `uv run pytest tests/test_routing.py::test_help_command -q`

Python is pinned to `>=3.14` (see `pyproject.toml` / `.python-version`); use `uv` for all invocations.

Env vars: `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN` (each overrides the matching `*_bot_token` in config.yaml; the corresponding frontend starts only when a token is available), `METALCLAW_CONFIG` (override config path). `OLLAMA_URL` and `FASTMAIL_API_TOKEN` env vars take precedence over `config.yaml` if set. Docker via `compose.yaml` runs `bot.py --daemon` and proxies Ollama through `host.docker.internal`.

## Configuration

YAML config search order: `METALCLAW_CONFIG` env var → `./config.yaml` in cwd (handy for dev) → `$XDG_CONFIG_HOME/metalclaw/config.yaml` (or `~/.config/metalclaw/config.yaml`). See `config.example.yaml` for fields: `vault_path`, `memory_subdir`, `fastmail_api_token`, `telegram_bot_token`, `discord_bot_token`, `discord_chat_channels`, `discord_heartbeat_channel`, `ollama_url`, `model`. The file is `.gitignore`d. Loaded lazily on first `get_config()` call (cached).

## Architecture

- **registry.py** — `@tool(description=..., parameters=...)` decorator and global `TOOLS` dict. Tools auto-register on import.
- **tools.py** — Tool implementations (weather, dice, train departures, mail, memory). Imported at runtime in `bot.py`'s `_async_main()` to trigger registration — never call tool functions before that import.
- **bot.py** — Thin entrypoint. Parses args, sets up logging, orchestrates the three frontends + heartbeat scheduler in `_async_main`, and re-exports a small set of private helpers (`_parse_command`, `_chat_with_provider`, `_active_session_messages`, `_split_system`, `_run_tool`, `_discord_scope_for`, `_strip_bot_mention`, `_split_for_discord`, `_DISCORD_MAX_MESSAGE`, `_DiscordChannel`) so the existing test suite keeps importing them as `bot._foo` without modification. Flags: `--daemon` (no REPL), `--no-telegram`, `--no-discord` (skip the matching frontend even if a token is configured).
- **chat_loop.py** — Provider-agnostic chat loop. `chat()` and `chat_via_escalation()` run the tool-call loop against the configured `providers.Provider`, mutating `messages` in place. `chat()` accepts an `on_tool_call` callback so frontends can render tool activity however they like. `build_system_prompt(now)` and `_refresh_system_prompt(messages)` inject the memory summary into the system message; `_chat_with_provider` also rebuilds the system prompt mid-loop after any memory-mutating tool call so the model sees its own writes within the same turn. Also hosts `_parse_command` (slash parser) and `_split_thinking` (strips `<think>...</think>` tags). No frontend dependencies.
- **frontends/** — One module per frontend, plus shared helpers.
  - `frontends/common.py` — Heartbeat-scope-string helpers (`telegram_scope`, `discord_scope`, `parse_*_scope`) used by `heartbeat.py` to route alerts back to the right frontend (memory itself has no scope), `ONBOARDING_STEPS` + `format_interests`, the tool-command parsers/formatters (`parse_train_args` … `format_search_result`) and the `TOOL_COMMANDS` registry, and the async `run_*` slash-command runners (`run_remember`, `run_forget`, `run_memory`, `run_heartbeat`, `run_big`, `run_onboard_start`, `run_onboard_answer`) used by both Telegram and Discord. Each runner takes a `send: SendFn` callback so the frontend keeps its own reply mechanism.
  - `frontends/cli.py` — REPL (`run_cli_repl`) using `prompt_toolkit` + `rich`. Holds CLI-only state (`_show_thinking`, `_pending_onboarding`, `_cli_messages`, `_COMMAND_HANDLERS`, `_CLIChannel`). Tool slash commands wrap the shared parsers/formatters in `_print_bot_markdown`. CLI's `/remember` `/forget` `/memory` `/heartbeat` `/big` `/onboard` stay sync + Rich-styled rather than delegating to `common.run_*` (the styling and sync REPL flow don't translate cleanly).
  - `frontends/telegram.py` — Per-`chat_id` session map in `_telegram_sessions`, onboarding state in `_telegram_onboarding`, known-chat persistence at `$XDG_DATA_HOME/metalclaw/telegram_chats.json`. `_TelegramChannel` registers with `channels.register()` for heartbeat fan-out. Slash dispatch delegates the cross-frontend commands to `frontends.common.run_*` via `_send_for(update)`.
  - `frontends/discord.py` — Per-`channel.id` session map in `_discord_sessions`, onboarding state in `_discord_onboarding`. Same shape as Telegram. Replies are sent as raw CommonMark and split by `_split_for_discord` to fit Discord's 2000-char limit, reopening fenced code blocks across cuts.
- **config.py** — YAML config loader with env-var overrides. `get_config()` is `lru_cache`d; `reset_cache()` for tests.
- **memory.py** — Single-user long-term memory in Obsidian-flavoured markdown at `<vault>/<memory_subdir>/memory.md`. Sections: `## Preferences` (`- **key**: value`), `## Facts`, `## Instructions`. Use `[[wikilinks]]` freely. Writes are atomic (tempfile + `os.replace`) and protected by a per-process `threading.Lock` plus `fcntl.flock` on a sidecar `memory.md.lock` so concurrent processes (e.g. `--daemon` and an interactive `bot.py`) never tear the file. Metalclaw is single-user — no scope abstraction.
- **history.py** — `SQLiteHistory` (prompt_toolkit `History` subclass) at `$XDG_DATA_HOME/metalclaw/history.db`. User inputs persist via `store_string`; assistant replies need explicit `save_assistant`.
- **self_change.py** — Spawns `claude -p` with `--allowedTools Edit,Write,Read`, snapshots pre-existing dirty/untracked files so reject reverts only Claude's delta, runs `task lint/build/test`, then prompts approve / approve! / reject / diff. Approved entries appended to `changes.jsonl`.

### Adding a new tool

Define a function in `tools.py` decorated with `@tool(...)`. Schema follows Ollama's OpenAI-compatible function-calling format. To expose it as a slash command, add a parser + formatter in `frontends/common.py` and wire it into `common.TOOL_COMMANDS` — that single registry is consumed by all three frontends (CLI wraps the formatter in `_print_bot_markdown`; Telegram/Discord send the formatted string directly).

### Self-modification flow

`/add-tool` and `/self-edit` → `self_change.run_self_change(request, REPO_ROOT)` → Claude subprocess edits files → lint+build+test gates → interactive approve/reject. `approve!` overrides failing checks. Reject uses `git checkout --` for tracked changes and unlinks new untracked files (only those Claude introduced).

### Memory system

Single shared memory file (`memory.py`). The model can read/write long-term memory via five tools: `set_user_preference` (key/value prefs), `add_user_fact` (free-form facts about the user), `add_user_instruction` (durable behavioural rules the assistant must follow on every turn — stored in `## Instructions`), `forget_user_memory` (delete by substring; refuses on ambiguous match), and `get_user_memory` (read the full file). A short summary is injected into the system prompt and refreshed at the start of every user turn (`_refresh_system_prompt`) and again mid-loop in `_chat_with_provider` whenever a memory-mutating tool runs, so the model sees its own writes within the same turn. `summary()` truncates at 600 chars by default and appends a "(call get_user_memory for full memory)" hint when the full text overflows, signalling the model to fetch more. Slash commands `/remember <key>=<value>`, `/forget <substring>`, and `/memory` are wired into all three frontends; Telegram and Discord share `frontends.common.run_remember/run_forget/run_memory` while CLI keeps its Rich-styled implementations.

`memory.migrate_legacy_scopes()` runs once on bot startup, merging any pre-collapse `cli.md` / `telegram-<chat_id>.md` / `discord-<channel_id>.md` siblings into `memory.md` and renaming the originals to `*.bak`. Idempotent — no-op once the legacy files are gone.

### Discord frontend

Each Discord channel — DM or guild channel — gets its own short-term session (conversation history); long-term memory is shared across all channels and frontends via the single `memory.md` file. The bot decides whether to respond to a non-command message via `_discord_should_respond()`:
- DM: always reply.
- Guild channel ID listed in `discord_chat_channels`: always reply (these are dedicated conversation threads).
- Other guild channel: reply only if `@bot` is mentioned, or the message is a reply to one of the bot's own messages.
Slash commands (`/help`, `/new`, `/remember …`, `/onboard`, etc.) trigger regardless of mention. Replies are sent as raw CommonMark (Discord renders it natively) and split via `_split_for_discord` to fit Discord's 2000-char limit, reopening fenced code blocks across cuts. Heartbeats addressed to any `discord-…` scope post to the single channel set in `discord_heartbeat_channel`; no Discord heartbeat goes anywhere if that field is unset.
