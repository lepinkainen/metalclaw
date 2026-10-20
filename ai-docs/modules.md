# modules

Per-module symbol map. `→` = imports. `←` = imported by.

## bot.py — entrypoint

- Public: `main()`, `_async_main()`.
- Re-exports for tests: see `architecture.md#re-exports`.
- → `heartbeat`, `memory`, `chat_loop`, `config`, `frontends.cli`, `frontends.telegram`, `frontends.discord`, `argparse`, `asyncio`, `logging`, `discord`, `telegram.ext.Application`.
- ← (lazy) `tools.escalate_to_big_model:784`, `heartbeat.run_tick:243`.

## chat_loop.py — provider-agnostic loop

- Public: `Session` (dataclass: `system`, `history: list[Message]`; `append_user`, `append_exchange`, `snapshot`), `new_session()`, `chat(session, on_tool_call=None) -> str`, `chat_via_escalation(session) -> str`, `run_turn(session, text, chat_call)`, `build_system_prompt(now) -> str`, `_parse_command`, `_split_thinking`, `_refresh_system_prompt(session)`, `_run_tool`, `_chat_with_provider(provider, session, ...)`, `_active_session` (ContextVar[Session | None]).
- Constants: `_SYSTEM_PROMPT_BASE` (chat_loop.py:49), `_ESCALATION_HINT` (chat_loop.py:67), `_MEMORY_MUTATORS = {"set_user_preference","add_user_fact","add_user_instruction","forget_user_memory"}` (chat_loop.py:96).
- → `memory`, `config`, `messages`, `providers`, `registry`.

## messages.py — canonical messages

- Frozen dataclasses `UserMessage`, `AssistantMessage(text, tool_calls, usage)`, `ToolResultMessage(call_id, name, content)`, `ToolCall`, `Usage`; `Message` union; `validate_exchange(assistant, results)`.
- No project imports; imported by `providers`, `chat_loop`, tests.

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

- Public: `run(stop)`, `run_tick(*, now=None)`, `load_ledger()`, `create_action(...)`, `list_active()`, `list_completed()`, `cancel(action_id)` (also `forget_state`), `load_state() -> dict[id, ActionState]`, `get_state(id)`, `mark_success(action, now, **meta)`, `mark_failure(action, now, reason, **meta)`, `forget_state(id)`, `prune_state(keep)`, `backoff_seconds(count)`, `is_action_due(action, last_run_iso, now)`, `next_due(action, last_run_iso, now)`, `summary(max_chars)`, `parse_interval(text)`, `normalise_weekdays(days)`, `validate_time_string(text)`, `validate_timezone(name)`, `parse_iso(text)` (naive → local), `format_now(dt=None)` (prompt clock with UTC offset), `SENTINEL = "HEARTBEAT_OK"`.
- Dataclasses: `ActionKind` (StrEnum), `CronSchedule`, `HeartbeatAction`, `HeartbeatLedger`, `ActionState` (one `heartbeat_state` row; `retry_at()`).
- → `channels`, `config`, `history` (state rows), `yaml`, `asyncio`. (lazy) `chat_loop`.

## history.py — sqlite prompt history

- `SQLiteHistory(session)` extends `prompt_toolkit.history.History`. Methods: `load_history_strings()`, `store_string(s)`, `save_assistant(s)`.
- DB: `$XDG_DATA_HOME/metalclaw/history.db`. Tables `messages(id, session, ts, role, content)`, `usage(id, ts, provider, model, prompt_tokens, completion_tokens, cost_usd NULL)` `heartbeat_state(action_id PK, last_run, failure_count, last_failed, updated_at, meta JSON)` and `known_chats(frontend, chat_id PK, first_seen, last_seen, meta JSON)`. `ts` is UTC ISO.
- `session`: CLI = bare timestamp id; Telegram/Discord = `telegram-<chat_id>` / `discord-<channel_id>` (same strings as heartbeat scopes). `load_history_strings` excludes any session containing `-` so up-arrow history stays CLI-only.
- Module fns (shared locked connection): `record_message`, `search_messages(query, limit)` (LIKE, newest first), `record_usage`, `usage_since(iso)` (grouped by provider/model), and the heartbeat-agnostic row ops `heartbeat_state_all/get`, `heartbeat_mark_success/failure` (upsert; failure uses `RETURNING failure_count`), `heartbeat_state_forget/prune/import`. `_merge_json` dict-updates a JSON `meta` column on every write (None values skipped, unparseable JSON kept under `_corrupt_meta`). `known_chat_touch(frontend, id, meta) -> created`, `known_chat_ids(frontend)`, `known_chats_all(frontend?)`, `known_chat_forget`, `known_chats_import` (INSERT OR IGNORE). No import of `heartbeat` or any frontend (would be circular).

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

## wilma.py — Wilma (school portal) client

- Unofficial: speaks the web UI's cookie-session + JSON endpoints. Protocol mirrors github.com/aikarjal/wilmai (TS, MIT) — diff its changelog when Wilma breaks.
- Login: `GET /login` hidden fields → `SESSIONID` (or `GET /token` → `Wilma2LoginID`) → `POST /login` form → `Wilma2SID` cookie. Redirect with `loginfailed` = bad creds. Redirect page containing `mfa-formkey` → `POST /api/v1/accounts/me/mfa/otp/check` with stdlib `totp(cfg.wilma_totp_secret)`.
- Endpoints: `/api/v1/accounts/me/roles` (students; `passwd` role skipped; on 404/non-JSON falls back to `/!<n>/` links on the home page; 5xx/network errors propagate uncached), `/!<student>/messages/list[/archive|/outbox]`, `/!<student>/messages/<id>?format=json` (falls back to `/messages/<id>` HTML on 404).
- **One live session per account**: every login cancels the previous one, browser included. So: lazy login, module singleton `get_client()` shared across threads (`RLock` serialises requests), re-login only on 401 / redirect to `/login` / 403-with-dead-session (`_session_alive` probes roles first so a single forbidden item doesn't log the parent out).
- Rejected creds or missing TOTP secret set `_failed` → all later calls raise immediately until restart (repeated failed logins can lock the account).
- Bodies: `ContentHtml` → markdown via `markdownify`, truncated at 20 000 chars. Timestamps → ISO in Europe/Helsinki.
- `match_messages(msgs, subject, sent_at)`: strips `Viesti Wilmasta:` prefix, prefix-matches (notification subjects are truncated), ±15 min window when `sent_at` given.
- `reset_client()` for tests; tests use `httpx.MockTransport(FakeWilma())` (`tests/test_wilma.py`).

## mcp_client.py — MCP client (phase 1)

- Public: `start(servers)`, `stop(timeout=5)`, `status() -> list[dict(name,status,transport,tools,error)]`, `system_prompt_note() -> str`.
- State: `_LOOP`/`_THREAD` (dedicated loop thread), `_STATE: dict[name, _Server]` (`cfg`, `status` starting/up/down/disabled, `tools` local names, `error`, `client`, `stop` Event, `task` concurrent future of `_serve`).
- `_transport(s, stack)` → `StdioServerParameters` or `streamable_http_client(url, http_client=httpx2.AsyncClient(headers, timeout))`; monkeypatched in tests to return an in-process `MCPServer`.
- `_serve` owner task: `Client` → paginated `list_tools` → `ready.set_result` → wait `stop`. `_register`, `_to_schema`, `_make_func`, `_content_text`, `_clip`, `_convert_result` — see `tools.md` "MCP tools".
- Deps in: `bot`, `chat_loop`, `frontends.common`. Tests: `tests/test_mcp_client.py`.

## telegram_format.py — CommonMark → Telegram HTML

- Public: `to_html(text) -> str`. Used only by `frontends/telegram.py`.
- Supported tags: `<b>`, `<i>`, `<u>`, `<s>`, `<code>`, `<pre>`, `<a>`, `<blockquote>`, `<tg-spoiler>`. Lists/headings degrade to plain text + bullets.

## providers/

- `base.py` — `Provider` Protocol (`chat_once(history: Sequence[Message], tools, system)`); re-exports `AssistantMessage`, `ToolCall`, `Usage`, `Message` from `messages`.
- `__init__.py` — `get_provider(name, *, model_override=None)` factory. 2-branch: `ollama` | `litellm`.
- `ollama.py` — `OllamaProvider(url, model)`. `_to_wire` → Ollama dicts (`role=tool` + `name` per result, tool_calls without ids).
- `litellm_provider.py` — `LiteLLMProvider(model, *, aws_region=None, aws_profile=None, num_retries=2)`. Uses `litellm.completion()` (OpenAI-shaped envelope across all backends). Module-level `litellm.drop_params=True` strips per-model unsupported kwargs. Tool-arg JSON parsed defensively; text content blocks joined, other blocks rejected. `_to_wire` → OpenAI envelope (`tool_call_id`, `arguments` as JSON string).

## frontends/

- `__init__.py` — empty.
- `common.py` — scope helpers (`telegram_scope`, `discord_scope`, `parse_*_scope`, prefixes), arg parsers/formatters, `TOOL_COMMANDS` registry, `HELP_LINES`, async `run_remember`/`run_forget`/`run_memory`/`run_heartbeat`/`run_big`. Each `run_*` takes a `SendFn = Callable[[str], Awaitable[None]]`.
- `cli.py` — `run_cli_repl()`, `_CLIChannel`, sync `_COMMAND_HANDLERS`, `_print_bot_markdown`, `_show_thinking` flag, `_cli_messages` global, REPO_ROOT for self_change. Uses `prompt_toolkit.PromptSession` + `rich.Console`.
- `telegram.py` — `start_telegram(token)`, `stop_telegram(app)`, `_TelegramChannel(app)`, `_get_telegram_session(chat_id)`, `_typing(chat_id, bot)` async ctxmgr (4-second pulse), `_load_known_chats()` (reads `known_chats` rows, runs the one-time `telegram_chats.json` migration), `_remember_chat(chat_id, meta)` → `history.known_chat_touch`, `_chat_meta(update)`.
- `discord.py` — `start_discord(token)`, `stop_discord(client, task)`, `_DiscordChannel(client, heartbeat_channel_id)`, `_split_for_discord(text, limit=2000)` (paragraph→line→word→hard-cut, fence-aware), `_discord_should_respond(message, bot_user)` gating, `_strip_bot_mention(text, bot_user_id)`, `_load_known_channels()` / `_remember_channel(channel_id, meta)` / `_channel_meta(message)` (`known_chats` rows, frontend=`discord`).
