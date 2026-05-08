# modules

Per-module symbol map. `→` = imports. `←` = imported by.

## bot.py — entrypoint

- Public: `main()`, `_async_main()`.
- Re-exports for tests: see `architecture.md#re-exports`.
- → `heartbeat`, `memory`, `chat_loop`, `config`, `frontends.cli`, `frontends.telegram`, `frontends.discord`, `argparse`, `asyncio`, `logging`, `discord`, `telegram.ext.Application`.
- ← (lazy) `tools.escalate_to_big_model:784`, `heartbeat.run_tick:243`.

## chat_loop.py — provider-agnostic loop

- Public: `chat(messages, on_tool_call=None) -> str`, `chat_via_escalation(messages) -> str`, `build_system_prompt(now) -> str`, `_parse_command`, `_split_system`, `_split_thinking`, `_refresh_system_prompt`, `_run_tool`, `_chat_with_provider`, `_active_session_messages` (ContextVar).
- Constants: `_SYSTEM_PROMPT_BASE` (chat_loop.py:49), `_ESCALATION_HINT` (chat_loop.py:67), `_MEMORY_MUTATORS = {"set_user_preference","add_user_fact","add_user_instruction","forget_user_memory"}` (chat_loop.py:96).
- → `memory`, `config`, `providers`, `registry`.

## registry.py — tool registry

- Public: `tool(*, description, args=None)` decorator, `TOOLS: dict[str, Tool]`, `Tool` dataclass.
- `_schema_from_model(model)` strips pydantic `title`, `$defs`, collapses `Optional` `anyOf`. `_EMPTY_PARAMETERS` for zero-arg tools.
- → `pydantic.BaseModel`.

## channels.py — pub/sub for proactive messages

- `Channel` Protocol: `name: str`, `async notify(scope, text)`, `active_scopes() -> Iterable[str]`.
- Globals: `CHANNELS: dict[str, Channel]`. Funcs: `register`, `unregister`, `for_scope(scope)`, `all_active_scopes()`.
- Routing: `cli` → `CHANNELS["cli"]`; `telegram-*` → `CHANNELS["telegram"]`; otherwise prefix-before-`-`.

## config.py — config loader

- Public: `Config` (frozen pydantic), `get_config()` (lru_cache 1), `reset_cache()`, `xdg_data_dir()`, `Provider` (Literal alias).
- Search order: `METALCLAW_CONFIG` → `./config.yaml` → `$XDG_CONFIG_HOME/metalclaw/config.yaml`.
- Env overrides: see `_ENV_OVERRIDES` (`config.py:30`).
- Validation: `_resolve_and_check` enforces api-key presence per active provider; defaults `escalation_model` from per-provider model when unset.
- → `pydantic`, `yaml`. **Leaf** — no project imports.

## memory.py — long-term memory

- Public: `load(*, copy=True) -> Memory`, `render_full() -> str`, `set_preference(k, v)`, `add_fact(t)`, `add_instruction(t)`, `forget(matcher) -> ForgetResult`, `migrate_legacy_scopes() -> list[str]`, `summary(max_chars=600) -> str`.
- Dataclasses: `Memory`, `ForgetResult`. Enums: `ForgetStatus`, `_CandidateKind`.
- Internal: `_path()`, `_parse(text)`, `_render(mem)`, `_locked()` ctxmgr, `_read_locked()`, `_write_locked(mem)` (atomic temp+replace), `_mutate(fn, log_fmt, *args)`.
- Cache: mtime-keyed (`_CACHE`), invalidated on write.
- → `config`. **No frontend deps.**

## heartbeat.py — scheduler

- Public: `run(stop)`, `run_tick(*, now=None)`, `parse_heartbeat_file(text)`, `parse_interval(text)`, `discover_scopes()`, `heartbeat_path_for(scope)`, `load_state()`, `save_state(state)`, `is_due(state, scope, task, now)`, `state_key(scope, name)`, `SENTINEL = "HEARTBEAT_OK"`.
- Dataclasses: `HeartbeatTask`, `HeartbeatFile`.
- → `channels`, `config`, `yaml`, `asyncio`. (lazy) `bot`.

## history.py — sqlite prompt history

- `SQLiteHistory(session)` extends `prompt_toolkit.history.History`. Methods: `load_history_strings()`, `store_string(s)`, `save_assistant(s)`.
- DB: `$XDG_DATA_HOME/metalclaw/history.db`. Single table `messages(id, session, ts, role, content)`.

## self_change.py — `/add-tool` `/self-edit`

- Public: `run_self_change(request, repo_root) -> SelfChangeResult`.
- Spawns `claude -p ... --allowedTools Edit,Write,Read` (timeout 300s). Snapshots dirty/untracked pre-run; rejects only revert delta.
- Gates: `task lint`, `task build`, `task test`. `approve!` overrides.
- Logs to `changes.jsonl`.

## tools.py — tool implementations

- Decorated with `@tool(...)`; auto-register on import. See `tools.md` for catalog.
- → `httpx`, `pydantic`, `memory`, `vault_search`, `config`, `registry`. (lazy) `bot`, `providers` (escalate only).
- HTTP client: module-level `_HTTP = httpx.Client(...)` (`tools.py:27`).
- Fastmail session/mailboxes cached at `_FM_SESSION`, `_FM_MAILBOXES` (lazy).

## vault_search.py — Obsidian search

- Public: `search(query, max_results=20, context_lines=1) -> dict`, `read(path) -> dict`.
- `search` shells `rg --json --type md --smart-case` with cwd at vault root. Honors `cfg.vault_search_excludes` (each becomes `--glob !pat`).
- `read` refuses path traversal (`is_relative_to(vault)`) and non-`.md` suffixes.
- Trims: `_LINE_CHAR_LIMIT = 300`, `_BODY_CHAR_LIMIT = 50_000`.

## telegram_format.py — CommonMark → Telegram HTML

- Public: `to_html(text) -> str`. Used only by `frontends/telegram.py`.
- Supported tags: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a>`, `<blockquote>`, `<tg-spoiler>`. Lists/headings degrade to plain text + bullets.

## providers/

- `base.py` — `Provider` Protocol, `AssistantMessage(text, tool_calls, raw)`, `ToolCall(id, name, arguments)`.
- `__init__.py` — `get_provider(name, *, model_override=None)` factory. 2-branch: `ollama` | `litellm`.
- `ollama.py` — `OllamaProvider(url, model)`. Native tool-calls dict shape; `format_tool_results` → `role=tool` per result.
- `litellm_provider.py` — `LiteLLMProvider(model, *, aws_region=None, aws_profile=None, num_retries=2)`. Uses `litellm.completion()` (OpenAI-shaped envelope across all backends). Module-level `litellm.drop_params=True` strips per-model unsupported kwargs. Tool-arg JSON parsed defensively. `format_tool_results` uses `tool_call_id` envelope.

## frontends/

- `__init__.py` — empty.
- `common.py` — scope helpers (`telegram_scope`, `discord_scope`, `parse_*_scope`, prefixes), arg parsers/formatters, `TOOL_COMMANDS` registry, `HELP_LINES`, async `run_remember`/`run_forget`/`run_memory`/`run_heartbeat`/`run_big`. Each `run_*` takes a `SendFn = Callable[[str], Awaitable[None]]`.
- `cli.py` — `run_cli_repl()`, `_CLIChannel`, sync `_COMMAND_HANDLERS`, `_print_bot_markdown`, `_show_thinking` flag, `_cli_messages` global, REPO_ROOT for self_change. Uses `prompt_toolkit.PromptSession` + `rich.Console`.
- `telegram.py` — `start_telegram(token)`, `stop_telegram(app)`, `_TelegramChannel(app)`, `_get_telegram_session(chat_id)`, `_typing(chat_id, bot)` async ctxmgr (4-second pulse), `_load_known_chats()`/`_save_known_chats()` JSON at `$XDG_DATA_HOME/metalclaw/telegram_chats.json`.
- `discord.py` — `start_discord(token)`, `stop_discord(client, task)`, `_DiscordChannel(client, heartbeat_channel_id)`, `_split_for_discord(text, limit=2000)` (paragraph→line→word→hard-cut, fence-aware), `_discord_should_respond(message, bot_user)` gating, `_strip_bot_mention(text, bot_user_id)`.
