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
- **bot.py** — Single entrypoint hosting all three frontends. Ollama chat client with tool-call loop (`chat()`), CLI REPL using `prompt_toolkit` + `rich`, the Telegram frontend (per-`chat_id` session map in `_telegram_sessions`, onboarding state in `_telegram_onboarding`), and the Discord frontend (per-`channel.id` session map in `_discord_sessions`, onboarding state in `_discord_onboarding`). `chat()` mutates `messages` in place and accepts an `on_tool_call` callback so frontends can render tool activity however they like. `build_system_prompt(scope, now)` injects a memory summary into the system message at session start. Flags: `--daemon` (no REPL), `--no-telegram`, `--no-discord` (skip the matching frontend even if a token is configured).
- **config.py** — YAML config loader with env-var overrides. `get_config()` is `lru_cache`d; `reset_cache()` for tests.
- **memory.py** — Per-scope user memory in Obsidian-flavoured markdown at `<vault>/<memory_subdir>/<scope>.md`. Scopes: `cli` for the REPL, `telegram-<chat_id>` for each Telegram chat, `discord-<channel_id>` for each Discord DM or guild channel (each Discord channel is its own conversation). Selected via `current_scope` `ContextVar`. Sections: `## Preferences` (`- **key**: value`), `## Facts`, `## Instructions`. Use `[[wikilinks]]` freely.
- **history.py** — `SQLiteHistory` (prompt_toolkit `History` subclass) at `$XDG_DATA_HOME/metalclaw/history.db`. User inputs persist via `store_string`; assistant replies need explicit `save_assistant`.
- **self_change.py** — Spawns `claude -p` with `--allowedTools Edit,Write,Read`, snapshots pre-existing dirty/untracked files so reject reverts only Claude's delta, runs `task lint/build/test`, then prompts approve / approve! / reject / diff. Approved entries appended to `changes.jsonl`.

### Adding a new tool

Define a function in `tools.py` decorated with `@tool(...)`. Schema follows Ollama's OpenAI-compatible function-calling format. To expose it as a slash command, add a parser + formatter in `bot.py` and wire it into `_COMMAND_HANDLERS` (and `_TELEGRAM_TOOL_COMMANDS` if it should also work over Telegram).

### Self-modification flow

`/add-tool` and `/self-edit` → `self_change.run_self_change(request, REPO_ROOT)` → Claude subprocess edits files → lint+build+test gates → interactive approve/reject. `approve!` overrides failing checks. Reject uses `git checkout --` for tracked changes and unlinks new untracked files (only those Claude introduced).

### Memory system

The model can read/write long-term memory via four tools (`set_user_preference`, `add_user_fact`, `forget_user_memory`, `get_user_memory`). The active scope is set via `memory.current_scope` `ContextVar`: the CLI REPL sets `cli` once at startup; Telegram handlers set `telegram-<chat_id>` per incoming update; Discord handlers set `discord-<channel_id>` per incoming message. A short summary of memory is injected into the system prompt at session start; mid-session writes show up to the model as inline tool results but `messages[0]` is not rewritten — updated memory enters the system prompt on the next session. Slash commands `/remember <key>=<value>`, `/forget <substring>`, `/memory`, and `/onboard` (4-question seeding flow) are wired into all three frontends.

### Discord frontend

Each Discord channel — DM or guild channel — gets its own session and its own `discord-<channel_id>.md` memory file. The bot decides whether to respond to a non-command message via `_discord_should_respond()`:
- DM: always reply.
- Guild channel ID listed in `discord_chat_channels`: always reply (these are dedicated conversation threads).
- Other guild channel: reply only if `@bot` is mentioned, or the message is a reply to one of the bot's own messages.
Slash commands (`/help`, `/new`, `/remember …`, `/onboard`, etc.) trigger regardless of mention. Replies are sent as raw CommonMark (Discord renders it natively) and split via `_split_for_discord` to fit Discord's 2000-char limit, reopening fenced code blocks across cuts. Heartbeats addressed to any `discord-…` scope post to the single channel set in `discord_heartbeat_channel`; no Discord heartbeat goes anywhere if that field is unset.
