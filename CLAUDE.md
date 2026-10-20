# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Deep architecture docs (AI-only)

Detailed architecture, dataflow, conventions, and gotchas live in `ai-docs/` — written for AI consumption (dense, indexed, file:line refs). Start at `ai-docs/INDEX.md`. Load topic-specific files when editing the matching area:

- `ai-docs/architecture.md` — module graph, layer rules, lifecycle
- `ai-docs/modules.md` — per-file symbol map
- `ai-docs/chat-loop.md` — `_chat_with_provider` deep dive, escalation
- `ai-docs/tools.md` — registration mechanics, full tool catalog, slash wiring
- `ai-docs/providers.md` — Provider Protocol, per-provider quirks
- `ai-docs/memory.md` — file format, locking, atomic write, forget semantics
- `ai-docs/frontends.md` — CLI/Telegram/Discord delta, sessions, gating
- `ai-docs/heartbeat.md` — scope file format, scheduler, sentinel
- `ai-docs/config.md` — field table, env overrides, validators
- `ai-docs/dataflow.md` — sequence diagrams (user turn, escalation, heartbeat, memory write)
- `ai-docs/conventions.md` — house rules
- `ai-docs/gotchas.md` — circular imports, ContextVar, Anthropic shape, etc.
- `ai-docs/testing.md` — pytest fixtures, common stubs, CI gates

Prefer these over re-reading the source for orientation. Update them when changing the matching area.

## Project

Metalclaw is a single-user chatbot that talks to an LLM provider — Ollama (default, `gemma4:latest`, local hot path) or any cloud provider via litellm (Bedrock, OpenAI, Anthropic, Gemini, …) — with tool calling, exposed via a single executable (`bot.py`) that runs the CLI REPL, the Telegram frontend, and the Discord frontend together. It also has a controlled self-modification path that shells out to `claude -p` to edit its own source.

## Commands

```
task build        # Import-check all modules
task lint         # ruff check . + ruff format . + mypy . + optional gitleaks scan
task test         # pytest -q
task daemon       # uv run python bot.py --daemon  (Telegram + Discord + heartbeat, no REPL)
uv run python bot.py                # CLI REPL + Telegram + Discord (each starts only if token is set)
uv run python bot.py --no-telegram  # skip Telegram
uv run python bot.py --no-discord   # skip Discord
```

Logs: DEBUG-level rotating file at `$XDG_STATE_HOME/metalclaw/metalclaw.log` (default `~/.local/state/metalclaw/metalclaw.log`), always on. Console is INFO in `--daemon`, WARNING in the REPL; set `METALCLAW_LOG_LEVEL=DEBUG|INFO|...` to override the console level. See `ai-docs/conventions.md` for logging rules.

Run a single test: `uv run pytest tests/test_routing.py::test_help_command -q`

Python is pinned to `>=3.14` (see `pyproject.toml` / `.python-version`); use `uv` for all invocations.

Env vars: `TELEGRAM_BOT_TOKEN`, `DISCORD_BOT_TOKEN` (each overrides the matching `*_bot_token` in config.yaml; the corresponding frontend starts only when a token is available), `METALCLAW_CONFIG` (override config path), `GITHUB_MCP_TOKEN` (or any var referenced as `${VAR}` in `mcp_servers` headers/env — expanded at connect time (a missing var marks just that server down, the bot runs on); for GitHub use a fine-grained PAT with read-only Metadata/Contents/Issues/Actions/Pull requests). `OLLAMA_URL`, `FASTMAIL_API_TOKEN` and `WILMA_URL`/`WILMA_USERNAME`/`WILMA_PASSWORD`/`WILMA_TOTP_SECRET` env vars take precedence over `config.yaml` if set. Cloud LLM credentials (AWS for Bedrock, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, etc.) are read directly by litellm — not via metalclaw's `_ENV_OVERRIDES`. Docker via `compose.yaml` runs `bot.py --daemon` and proxies Ollama through `host.docker.internal`; the image installs `ripgrep` but not the `claude` CLI, so set `allow_self_modification: false` in config.yaml for container deployments. Compose bind-mounts the vault (`${METALCLAW_VAULT:-./vault}` → `/vault`; config.yaml must set `vault_path: /vault`) and `config.yaml` (`${METALCLAW_CONFIG_FILE:-./config.yaml}` → `/config/config.yaml`, read-only, via `METALCLAW_CONFIG`), and keeps `XDG_DATA_HOME=/data` (history.db: chat history, usage, heartbeat state, known chats) and `XDG_STATE_HOME=/state` (logs) in named volumes.

## Configuration

YAML config search order: `METALCLAW_CONFIG` env var → `./config.yaml` in cwd (handy for dev) → `$XDG_CONFIG_HOME/metalclaw/config.yaml` (or `~/.config/metalclaw/config.yaml`). See `config.example.yaml` for the full schema: `vault_path`, `memory_subdir`, `fastmail_api_token`, `wilma_url`, `wilma_username`, `wilma_password`, `wilma_totp_secret`, `telegram_bot_token`, `discord_bot_token`, `discord_chat_channels`, `discord_heartbeat_channel`, `provider` (`ollama` | `litellm`), `ollama_url`, `model`, `litellm_model`, `aws_region`, `aws_profile`, `escalation_*`, `heartbeat_*`, `vault_search_excludes`, `message_debounce_seconds`, `mcp_servers`. Env vars `FASTMAIL_API_TOKEN` / `OLLAMA_URL` / `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` override the matching yaml field. AWS / cloud LLM credentials are read directly by litellm (boto3 chain for Bedrock; `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` etc. for the others) — not via `_ENV_OVERRIDES`. The file is `.gitignore`d. Loaded lazily on first `get_config()` call (cached); call `config.reset_cache()` in tests.

## Architecture

- **registry.py** — `@tool(description=..., parameters=...)` decorator and global `TOOLS` dict. Tools auto-register on import.
- **tools/** — Tool implementations split by domain: `dice.py` (`roll_die`), `weather.py`, `trains.py` (`train_departures`), `mail.py` (`list_emails`/`read_email`/`check_new_mail`/`mark_emails_reported`/`list_email_labels`/`add_email_label`/`remove_email_label` via Fastmail JMAP), `memory_tools.py` (the five memory tools), `search.py` (`search_vault`/`read_note`), `inbox.py` (`append_to_inbox` — appends to `INBOX.md` in the vault root only), `conversation_history.py` (`search_conversation_history`), `wilma.py` (`list_wilma_messages`/`read_wilma_message`/`find_wilma_message`), and `escalation.py` (`escalate_to_big_model`). `tools/__init__.py` re-imports each submodule so registration side-effects fire on `import tools`. Shared `httpx.Client` lives in `tools/_http.py`. `bot.py`'s `_async_main()` imports the package at runtime to trigger registration — never call tool functions before that import.
- **providers/** — Provider abstraction. `providers.base.Provider` is a Protocol returning `AssistantMessage` (text + `ToolCall` list). Concrete impls: `ollama.py` (httpx → local Ollama, hot path) and `litellm_provider.py` (litellm-routed cloud — Bedrock, OpenAI, Anthropic, Gemini, …; OpenAI-shaped envelope; `litellm.drop_params=True` strips per-model unsupported kwargs). `providers.get_provider(name, model_override=...)` is the factory consumed by `chat_loop.py`. Selected via `provider:` in `config.yaml` (`ollama` | `litellm`). Model strings under litellm follow its prefix convention (`bedrock/<id>`, `openai/<id>`, `anthropic/<id>`, `gemini/<id>`).
- **bot.py** — Thin entrypoint. Parses args, sets up logging, orchestrates the three frontends + heartbeat scheduler in `_async_main`. Flags: `--daemon` (no REPL), `--no-telegram`, `--no-discord` (skip the matching frontend even if a token is configured). Tests import internals directly from `chat_loop` / `frontends.discord` / `frontends.common` rather than via `bot`.
- **chat_loop.py** — Provider-agnostic chat loop. Conversations live in a `Session` dataclass (`system: str`, `history: list[dict]` of provider-shaped messages, `provider_name: str | None`); frontends hold one per CLI / Telegram chat / Discord channel, heartbeat builds one per action run. `chat()` and `chat_via_escalation()` run the tool-call loop against the configured `providers.Provider`, working on a copy of `session.history` and committing it only on success (a provider exception leaves the session untouched). `chat()` accepts an `on_tool_call` callback so frontends can render tool activity however they like. `build_system_prompt(now)` and `_refresh_system_prompt(session)` rebuild `session.system` with the memory summary; `_chat_with_provider` also rebuilds it mid-loop after any memory-mutating tool call so the model sees its own writes within the same turn. `chat()` stamps `session.provider_name`; if `provider:` in config changes between turns on the same session, prior history (provider-specific wire format) is auto-dropped (current user turn preserved) so the new provider does not choke on foreign payloads. The stamp dies with the session, so `/new` just drops the object. Also hosts `_parse_command` (slash parser) and `_split_thinking` (strips `<think>...</think>` tags). No frontend dependencies.
- **frontends/** — One module per frontend, plus shared helpers.
  - `frontends/common.py` — Heartbeat-scope-string helpers (`telegram_scope`, `discord_scope`, `parse_*_scope`) used by `heartbeat.py` to route alerts back to the right frontend (memory itself has no scope), `ONBOARDING_STEPS` + `format_interests`, the tool-command parsers/formatters (`parse_train_args` … `format_search_result`) and the `TOOL_COMMANDS` registry, and the async `run_*` slash-command runners (`run_remember`, `run_forget`, `run_memory`, `run_heartbeat`, `run_big`, `run_onboard_start`, `run_onboard_answer`) used by both Telegram and Discord. Each runner takes a `send: SendFn` callback so the frontend keeps its own reply mechanism.
  - Live tool status: `common.format_tool_status` + `common.ToolStatusReporter` (thread-safe send-then-edit status message) feed `on_tool_call` in Telegram/Discord (and `run_big`); toggled per chat with `/tools [on|off]`, default `show_tool_calls` config. Heartbeat runs get no callback. CLI prints the same lines dim.
  - `frontends/cli.py` — REPL (`run_cli_repl`) using `prompt_toolkit` + `rich`. Holds CLI-only state (`_show_thinking`, `_pending_onboarding`, `_cli_messages`, `_COMMAND_HANDLERS`, `_CLIChannel`). Tool slash commands wrap the shared parsers/formatters in `_print_bot_markdown`. CLI's `/remember` `/forget` `/memory` `/heartbeat` `/big` `/onboard` stay sync + Rich-styled rather than delegating to `common.run_*` (the styling and sync REPL flow don't translate cleanly).
  - `frontends/telegram.py` — Per-`chat_id` session map in `_telegram_sessions`, onboarding state in `_telegram_onboarding`, known chats persisted in the `known_chats` table of `history.db` (frontend=`telegram`; `meta` JSON keeps chat type/title/username; a legacy `telegram_chats.json` is imported once and renamed `.bak`). `_TelegramChannel` registers with `channels.register()` for heartbeat fan-out. Slash dispatch delegates the cross-frontend commands to `frontends.common.run_*` via `_send_for(update)`.
  - `frontends/discord.py` — Per-`channel.id` session map in `_discord_sessions`, onboarding state in `_discord_onboarding`. Same shape as Telegram, including `known_chats` rows (frontend=`discord`, `meta` keeps channel/guild/author) so heartbeat fan-out survives restarts. Replies are sent as raw CommonMark and split by `_split_for_discord` to fit Discord's 2000-char limit, reopening fenced code blocks across cuts.
- **config.py** — YAML config loader with env-var overrides. `get_config()` is `lru_cache`d; `reset_cache()` for tests.
- **memory.py** — Single-user long-term memory in Obsidian-flavoured markdown at `<vault>/<memory_subdir>/memory.md`. Sections: `## Preferences` (`- **key**: value`), `## Facts`, `## Instructions`. Use `[[wikilinks]]` freely. Writes are atomic (tempfile + `os.replace`) and protected by a per-process `threading.Lock` plus `fcntl.flock` on a sidecar `memory.md.lock` so concurrent processes (e.g. `--daemon` and an interactive `bot.py`) never tear the file. Metalclaw is single-user — no scope abstraction.
- **history.py** — SQLite at `$XDG_DATA_HOME/metalclaw/history.db` behind one shared locked connection. `messages` table holds CLI (`SQLiteHistory`, prompt_toolkit up-arrow, CLI rows only), Telegram (`telegram-<chat_id>`) and Discord (`discord-<channel_id>`) turns, searchable via the `search_conversation_history` tool. `usage` table holds per-LLM-call tokens/cost (recorded in `chat_loop._chat_with_provider` from `AssistantMessage.usage`), shown by `/usage`.
- **channels.py** — `Channel` Protocol (`name`, `scopes()`, `send(scope, text)`) plus a process-global registry (`register` / `unregister` / `for_scope` / `all_active_scopes`). Frontends register their own channel objects on startup so `heartbeat.py` can fan alerts out without depending on any frontend module.
- **heartbeat.py** — Scheduled proactive checks driven by a bot-owned action ledger at `<vault>/<memory_subdir>/heartbeat.yaml`. Three action kinds: `at` (one-shot at a timestamp), `cron` (selected weekdays at one local time + IANA timezone), `every` (interval seconds). Actions are created/listed/cancelled by the model via `create_heartbeat_action` / `list_heartbeat_actions` / `cancel_heartbeat_action` (`tools/heartbeat_tools.py`); users do not edit the ledger by hand. Each tick (`run_tick`) iterates active actions, runs the due ones independently through `chat_loop.chat()`, and routes alerts to `action.channel` (or `cfg.heartbeat_default_channel` as fallback). Per-action last-run state and consecutive-failure counts live in the `heartbeat_state` table of `$XDG_DATA_HOME/metalclaw/history.db` (row ops in `history.py`, `heartbeat.ActionState` view; a JSON `meta` column carries debug context — kind, channel, prompt snippet, last outcome/error — merged on every write). Rows are upserted per action so concurrent ticks never lose updates; `cancel()` and end-of-tick pruning drop orphan rows and `_generate_id` never recycles an id with live state. A pre-existing `heartbeat_state.json` is imported once and renamed `.bak`. Any failed run or delivery (chat error, empty reply, no channel, `notify` raised) keeps the action active and backs off exponentially from one interval up to `heartbeat_failure_backoff_max_seconds` (default 3600) before the next model call; a success clears the backoff. Honors `heartbeat_active_hours` window. The model emits the literal `HEARTBEAT_OK` sentinel when nothing needs reporting; one-shots are archived to `completed:` only after a successful notify (failed notifies keep the action active). `chat_loop.current_scope` is a `ContextVar` pinned by each frontend before `chat()` runs, so `create_heartbeat_action` can auto-fill `channel` from the active surface. Mid-loop system-prompt refresh after `create_/cancel_heartbeat_action` mirrors the memory-mutator pattern.
- **vault_search.py** — Obsidian vault search (`search`) and note read (`read`) backing the `search_vault` / `read_note` tools. `search` shells out to `ripgrep` (must be on PATH), respects `vault_search_excludes` globs from config, and trims long lines/bodies (`_LINE_CHAR_LIMIT=300`, `_BODY_CHAR_LIMIT=50_000`). `/search <query>` slash command in all three frontends.
- **wilma.py** — Unofficial client for Wilma (Finnish school portal; protocol mirrors github.com/aikarjal/wilmai). Form login → `Wilma2SID` cookie, optional TOTP two-step via `wilma_totp_secret`, then JSON endpoints for students and messages. Wilma allows one session per account (a login cancels the browser's), so a lazy process-wide `get_client()` reuses the session and re-logs in only when Wilma says it's gone; rejected credentials disable further attempts until restart to avoid account lockout. `find_wilma_message` resolves "Viesti Wilmasta" notification emails (which carry only the subject) to the full message; `check_new_mail` tags such emails with `hint`. `/wilma` slash command in all three frontends.
- **mcp_client.py** — MCP client (phase 1). `start(cfg.mcp_servers)` (from `bot._async_main`, after `import tools`, before frontends) spawns one daemon thread with its own asyncio loop; each enabled server gets one owner task holding `async with mcp.Client(...)` (streamable HTTP via `httpx2` with configured headers, or stdio). Tools are listed once and written straight into `registry.TOOLS` as `<name>_<tool>` (native names win on collision; `include`/`exclude` filter) with sync closures that `run_coroutine_threadsafe` onto the MCP loop (safe from any thread, bounded by `call_timeout_seconds`). Results come back as `{source: "mcp:<name>", untrusted: true, result}` clipped at 20000 chars; server errors become `Error: ...` strings. A server that fails at startup is `down` and contributes no tools; no reconnect (restart to recover). `system_prompt_note()` adds the untrusted-output rule + per-server `hint` to `build_system_prompt` when MCP tools exist; `status()` feeds the MCP block in `/status`. First real server: GitHub's hosted remote, read-only (see `config.example.yaml`). Design + deferred work: `ai-docs/mcp-plan.md`.
- **telegram_format.py** — Markdown → Telegram-flavoured HTML via `markdown-it-py` (Telegram doesn't render CommonMark natively; Discord does). Used only by the Telegram frontend.
- **self_change.py** — Backs `/self-edit`. Spawns `claude -p` with `--allowedTools Edit,Write,Read`, snapshots pre-existing dirty/untracked files so reject reverts only Claude's delta, runs `task lint/build/test`, then prompts approve / approve! / reject / diff. Approved entries appended to `changes.jsonl`. CLI-only; takes effect on next bot launch.
- **live_tool.py** — Backs `/add-tool`. Same `claude -p` shell with a tighter contract: write exactly one new file at `tools/<slug>.py`, edit nothing else. Focused gates (ruff on the new file, `importlib.import_module` + register diff against `registry.TOOLS`, schema-shape sanity) — seconds, not minutes. On `/approve`, appends `from .<slug> import <names>` to `tools/__init__.py` and merges the names into `__all__` via an AST-aware idempotent edit. The new tool is callable in the *current* session because the import side-effect already mutated `registry.TOOLS` and `chat_loop.py` rebuilds the provider-facing schema list every turn. `/reject` unlinks the file and pops the new keys from `TOOLS`. CLI + Telegram + Discord.

### Escalation

When `escalation_enabled: true` in config, the local model can call the `escalate_to_big_model` tool and the user can type `/big <query>` to bypass the local model entirely. `chat_via_escalation()` in `chat_loop.py` swaps in the escalation provider (`escalation_provider` + `escalation_model`) for that single turn. Default escalation = `litellm`; `escalation_model` defaults to the configured `litellm_model` (`bedrock/anthropic.claude-haiku-4-5` unless overridden — `config.example.yaml` sets Opus explicitly); swap `escalation_model` to any litellm-supported string (`bedrock/amazon.nova-pro-v1:0`, `bedrock/meta.llama3-70b-instruct-v1:0`, `openai/gpt-4o`, …) to A/B different models without touching code. Useful for keeping cheap local inference as the default while having a cloud fallback for hard questions.

### Adding a new tool

Drop a new module under `tools/` decorating one or more functions with `@tool(...)` from `registry`. Schema follows Ollama's OpenAI-compatible function-calling format. Add the import + `__all__` entry to `tools/__init__.py` (or let `/add-tool` do it for you). To expose it as a slash command, add a parser + formatter in `frontends/common.py` and wire it into `common.TOOL_COMMANDS` — that single registry is consumed by all three frontends (CLI wraps the formatter in `_print_bot_markdown`; Telegram/Discord send the formatted string directly).

### Self-modification flow

Two paths, both shell out to `claude -p`:

- **`/add-tool <description>`** (live, CLI/Telegram/Discord) → `live_tool.run_add_tool_live` → constrained Claude run (one new `tools/<slug>.py`, no other edits) → focused gates (ruff + import + schema sanity) → async approval. The model registers immediately because importing the new module fires the `@tool` decorator; on `/approve` the import is also persisted to `tools/__init__.py`. On `/reject` the file is unlinked and the new keys are popped from `registry.TOOLS`.
- **`/self-edit <description>`** (CLI-only, restart-tied) → `self_change.run_self_change` → unconstrained Claude run anywhere in the repo → `task lint/build/test` gates → interactive approve/reject. `approve!` overrides failing gates. Reject uses `git checkout --` for tracked changes and unlinks new untracked files (only those Claude introduced).

Both `/add-tool` and `/self-edit` are gated on `allow_self_modification` in `config.yaml` (default `true`). Set it to `false` on any deployment where Telegram or Discord could be reached by anyone other than the operator — `/add-tool` is reachable from those frontends and registers arbitrary new Python under `tools/<slug>.py`.

Approval commands (slash form only — bare-word would collide with chatting "approve, looks good" to the model):

- `/approve` — accept if all gates passed.
- `/approve_force` (or `/approve-force` in CLI) — accept regardless.
- `/reject` — discard.
- `/diff` — show the diff, leave the pending change in place.

Approved entries (live-add or self-edit) are appended to `changes.jsonl`.

### Memory system

Single shared memory file (`memory.py`). The model can read/write long-term memory via five tools: `set_user_preference` (key/value prefs), `add_user_fact` (free-form facts about the user), `add_user_instruction` (durable behavioural rules the assistant must follow on every turn — stored in `## Instructions`), `forget_user_memory` (delete by substring; refuses on ambiguous match), and `get_user_memory` (read the full file). A short summary is injected into the system prompt and refreshed at the start of every user turn (`_refresh_system_prompt`) and again mid-loop in `_chat_with_provider` whenever a memory-mutating tool runs, so the model sees its own writes within the same turn. `summary()` truncates at 600 chars by default and appends a "(call get_user_memory for full memory)" hint when the full text overflows, signalling the model to fetch more. Slash commands `/remember <key>=<value>`, `/forget <substring>`, and `/memory` are wired into all three frontends; Telegram and Discord share `frontends.common.run_remember/run_forget/run_memory` while CLI keeps its Rich-styled implementations.

`memory.migrate_legacy_scopes()` runs once on bot startup, merging any pre-collapse `cli.md` / `telegram-<chat_id>.md` / `discord-<channel_id>.md` siblings into `memory.md` and renaming the originals to `*.bak`. Idempotent — no-op once the legacy files are gone.

### Message debounce, /status, /usage

Telegram and Discord buffer non-command messages per chat/channel (`common.Debouncer`, `message_debounce_seconds`, default 1.0, 0 disables) and run ONE turn on the joined text after the quiet period; commands are never delayed. `/status` (`common.format_status`) shows model, uptime, frontends, heartbeat next-due times and recent WARNING+ logs (ring buffer `common.recent_warnings`). `/usage` (`common.format_usage`) reports token usage/cost from the `usage` table. Details in `ai-docs/frontends.md`.

### Discord frontend

Each Discord channel — DM or guild channel — gets its own short-term session (conversation history); long-term memory is shared across all channels and frontends via the single `memory.md` file. The bot decides whether to respond to a non-command message via `_discord_should_respond()`:
- DM: always reply.
- Guild channel ID listed in `discord_chat_channels`: always reply (these are dedicated conversation threads).
- Other guild channel: reply only if `@bot` is mentioned, or the message is a reply to one of the bot's own messages.
Slash commands (`/help`, `/new`, `/remember …`, `/onboard`, etc.) trigger regardless of mention. Replies are sent as raw CommonMark (Discord renders it natively) and split via `_split_for_discord` to fit Discord's 2000-char limit, reopening fenced code blocks across cuts. Heartbeats addressed to any `discord-…` scope post to the single channel set in `discord_heartbeat_channel`; no Discord heartbeat goes anywhere if that field is unset.

### Tools/__init__.py — `tools/` re-imports

Tool modules currently re-imported by `tools/__init__.py`: `conversation_history`, `dice`, `escalation`, `heartbeat_tools`, `inbox`, `mail`, `manual`, `memory_tools`, `search`, `trains`, `weather`, `wilma`. Each `from .x import …` triggers `@tool` registration side-effects.
