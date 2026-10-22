# modules

Per-module symbol map. `→` = imports. `←` = imported by.

## bot.py — entrypoint

- Public: `main()`, `_async_main(*, with_telegram, with_discord)`, `_setup_logging() -> Path`. Headless: no REPL, no `--daemon` flag; waits on `asyncio.Event()` until Ctrl-C.
- No re-exports for tests (see `architecture.md#test-imports`).
- → `heartbeat`, `memory`, `mcp_client`, `config`, `frontends.common`, `frontends.telegram`, `frontends.discord`, `argparse`, `asyncio`, `logging`, `discord`, `telegram.ext.Application`.
- ← nothing imports `bot` (tests import `bot._setup_logging` only in `tests/test_logging_setup.py`).

## chat_loop.py — provider-agnostic loop

- Public: `Session` (dataclass `chat_loop.py:146`: `system`, `history: list[Message]`, `scope: str | None`, private `_persisted: int`; classmethod `load(scope)` `:168`, staticmethod `reset(scope)` `:180`, `commit(work)` `:185`, `append_user`, `append_exchange`, `snapshot`), `new_session()`, `chat(session, on_tool_call=None) -> str`, `chat_via_escalation(session) -> str`, `run_turn(session, text, chat_call)` `:534`, `scoped_chat(scope, fn)` `:259`, `build_system_prompt(now) -> str` `:121`, `_parse_command` `:74`, `_split_thinking` `:59`, `_refresh_system_prompt(session)` `:217`, `_run_tool` `:274`, `_chat_with_provider(provider, session, ...)` `:309`, `_active_session` (ContextVar[Session | None]) `:248`, `current_scope` (ContextVar[str | None]) `:256`.
- Constants: `_SYSTEM_PROMPT_BASE` (chat_loop.py:85), `_ESCALATION_HINT` (chat_loop.py:113), `_MEMORY_MUTATORS` (chat_loop.py:227; memory tools + `create_/cancel_heartbeat_action`).
- Persistence: `Session.load` → `history_db.turns_load`; `commit` → `history_db.turns_append(scope, work.history[_persisted:])` then adopts `work`; `reset` → `history_db.turns_reset`. Scope `None` → in-memory only.
- → `memory`, `config`, `messages`, `providers`, `registry`, `history` (imported as `history_db`), `heartbeat` (`format_now`, `summary`), `mcp_client`.

## messages.py — canonical messages

- Frozen dataclasses `UserMessage`, `AssistantMessage(text, tool_calls, usage)`, `ToolResultMessage(call_id, name, content)`, `ToolCall`, `Usage` (+ `cache_read_tokens`/`cache_write_tokens`); `Message` union; `validate_exchange(assistant, results)` `:94`.
- Serialisation: `message_to_dict(message) -> dict` `:117` (JSON-ready, `kind` tag `user`/`assistant`/`tool_result`; `Usage` dropped) and `message_from_dict(data) -> Message` `:141` (`ValueError` on unknown kind). Used by `history.turns_append/turns_load`.
- System-prompt split: `SYSTEM_CACHE_BOUNDARY` `:34`, `split_system_prompt` `:37`, `flatten_system_prompt` `:43`.
- No project imports; imported by `providers`, `chat_loop`, `history`, tests.

## registry.py — tool registry

- Public: `tool(*, description, args=None)` decorator, `TOOLS: dict[str, Tool]`, `Tool` dataclass.
- `_schema_from_model(model)` strips pydantic `title`, `$defs`, collapses `Optional` `anyOf`. `_EMPTY_PARAMETERS` for zero-arg tools.
- → `pydantic.BaseModel`.

## channels.py — pub/sub for proactive messages

- `Channel` Protocol: `name: str`, `async notify(scope, text)`, `active_scopes() -> Iterable[str]`.
- Globals: `CHANNELS: dict[str, Channel]`. Funcs: `register`, `unregister`, `for_scope(scope)`, `all_active_scopes()`.
- Routing (`channels.py:44 for_scope`): `CHANNELS.get(scope.split("-", 1)[0])` — prefix-before-`-`, no special cases (`telegram-<id>` → `"telegram"`, `discord-<id>` → `"discord"`).

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

- Public: `run(stop)`, `run_tick(*, now=None)`, `load_ledger()`, `create_action(...)`, `list_active()`, `list_completed()`, `cancel(action_id)` (records `cancelled_at` in row meta), `load_state() -> dict[id, ActionState]`, `get_state(id)`, `mark_success(action, now, **meta)`, `mark_failure(action, now, reason, **meta)`, `backoff_seconds(count)`, `is_action_due(action, last_run_iso, now)`, `next_due(action, last_run_iso, now)`, `summary(max_chars)`, `strip_sentinel(reply) -> (text, acked)`, `config_pipeline_actions() -> list[HeartbeatAction]` (from `cfg.heartbeat_pipelines`, ids `pipeline-<name>`, `created_from="config"`), `_run_pipeline_action(action, now)` (builds `PipelineContext`, runs registry fn, writes `pipeline_runs` row in `finally`), `parse_interval(text)`, `normalise_weekdays(days)`, `validate_time_string(text)`, `validate_timezone(name)`, `parse_iso(text)` (naive → local), `format_now(dt=None)` (prompt clock with UTC offset), `SENTINEL = "HEARTBEAT_OK"`.
- Dataclasses: `ActionKind` (StrEnum), `CronSchedule`, `HeartbeatAction` (+ `pipeline: str | None`; `kind` stays the schedule type), `HeartbeatLedger`, `ActionState` (one `heartbeat_state` row; `retry_at()`).
- → `channels`, `config`, `history` (state rows), `yaml`, `asyncio`. (lazy) `chat_loop`, `pipelines`.

## history.py — sqlite: sessions, usage, scheduler state, known chats, pipeline ledger + traces

- DB: `$XDG_DATA_HOME/metalclaw/history.db`, one shared connection behind `_LOCK`. Tables (`history.py:16 _init`): `turns(id, session, ts, kind, payload JSON)` + index `turns_session_id(session, id)` `:35-43`; legacy `messages(id, session, ts, role, content)` `:23` (read-only now); `usage(id, ts, provider, model, prompt_tokens, completion_tokens, cost_usd NULL)` `:45`; `heartbeat_state(action_id PK, last_run, failure_count, last_failed, updated_at, meta JSON)` `:56`; `known_chats(frontend, chat_id PK, first_seen, last_seen, meta JSON)` `:66`; `reported_items(source, external_id PK, outcome, first_reported_at)` (shared watcher ledger; `reported_ids(source, ids) -> set`, `reported_mark(source, ids, outcome, ts=None)` INSERT OR IGNORE — first outcome wins, `reported_count(source)`); `pipeline_runs(run_id PK, pipeline, action_id, started_at, finished_at, outcome, error, steps JSON)` + index `(pipeline, started_at)` (`pipeline_run_record(...)`, `pipeline_runs_latest()` one row per pipeline, `pipeline_runs_recent(pipeline, limit)`). `ts` is UTC ISO.
- `session` column = frontend scope string: `telegram-<chat_id>` / `discord-<channel_id>` (same strings as heartbeat scopes). Legacy `messages` rows from the removed CLI have bare-timestamp sessions.
- **turns** (`history.py:96-166`): `_BOUNDARY = "boundary"`; `_last_boundary_id(db, session)`; `turns_load(session) -> list[Message]` (rows after the latest boundary, `message_from_dict`); `turns_append(session, new)` (one `executemany` + commit, rollback on error — all rows or none; `kind` = dict `kind`, `payload` = `json.dumps(message_to_dict(m))`); `turns_reset(session)` (inserts a boundary row; nothing deleted); `turns_count(session)` (live rows). Only `chat_loop.Session` calls these.
- `search_messages(query, limit)` `:168` — LIKE (escaped) over a UNION ALL of `turns` (`kind IN ('user','assistant')`, text via `json_extract(payload, '$.content' | '$.text')`, empty text skipped — tool calls/results never match) and legacy `messages`; `ORDER BY ts DESC`.
- `record_usage`, `usage_since(iso)` (grouped by provider/model); heartbeat-agnostic row ops `heartbeat_state_all/get`, `heartbeat_mark_success/failure` (upsert; failure uses `RETURNING failure_count`), `heartbeat_merge_meta`, `heartbeat_state_import`; `_merge_json` dict-updates a JSON `meta` column on every write (None values skipped, unparseable JSON kept under `_corrupt_meta`). `known_chat_touch(frontend, id, meta) -> created`, `known_chat_ids(frontend)`, `known_chats_all(frontend?)`, `known_chats_import` (INSERT OR IGNORE).
- → `config` (`xdg_data_dir`), `messages`. No import of `chat_loop`, `heartbeat` or any frontend (would be circular). `record_message` and `SQLiteHistory` are gone.

## self_change.py — self-modification plumbing

- Public: `SelfModificationBusy`, `busy_slot()` ctxmgr (non-blocking acquire of `_BUSY: threading.Lock` `:24`, raises `SelfModificationBusy`), `is_busy()`.
- Helpers imported by `live_tool`: `_read_file`, `_run(cmd, cwd, timeout=60)` (logs `claude -p` argv truncated — prompt embeds source), `_dirty_tracked(repo_root)`, `_untracked(repo_root)`.
- No `run_self_change` any more (`/self-edit` was CLI-only and removed). → stdlib only.

## live_tool.py — `/add-tool`

- Public: `LiveAddState`, `ApprovalResult`, `run_add_tool_live(request, repo_root) -> LiveAddState` (`:61`, takes `busy_slot()`; busy → aborted state), `finalise_add_tool_live(state, choice)` (`:141`; `approve` / `approve-force` / `reject` / `diff`).
- Internals: `_build_prompt`, `_validate_delta` (exactly one new `tools/<slug>.py`, no tracked edits), `_revert_changes`, `_run_gates` (ruff on the file, import + `TOOLS` diff, schema sanity), `_append_to_init` / `_import_insert_line` / `_merge_into_all` (AST-aware idempotent `tools/__init__.py` edit), `_log_change` → `changes.jsonl` (`:373`).
- → `self_change`, `registry`. ← `frontends.common` (`run_add_tool` / `run_approve` / `run_reject` / `run_diff`, pending state in `_pending_self_change[scope]`).

## tools/ — tool implementations

- Decorated with `@tool(...)`; auto-register on import. See `tools.md` for catalog.
- → `httpx`, `pydantic`, `memory`, `vault_search`, `config`, `registry`, `history` (conversation_history), `heartbeat` (heartbeat_tools). (lazy) `chat_loop`, `providers` (escalation only).
- HTTP client: module-level `_HTTP = httpx.Client(...)` (`tools/_http.py`).
- Fastmail session/mailboxes cached at `_FM_SESSION`, `_FM_MAILBOXES` (lazy). `tools/mail.py` also hosts the pipeline helpers (`fetch_unread_heads`, `drop_labelled`, `fetch_bodies`, `attach_handler_hints`, `_jmap_post`, `migrate_legacy_ledger`, `LEDGER_SOURCE = "mail"`) — see `pipelines.md`.

## pipelines/ — watcher pipelines

- `context.py`: `Outcome` (StrEnum `suppressed`/`dismissed`/`delivered`), `Step`, `Trace` (`step(name)` ctx manager → `Step` with `set_note`; `as_rows()`), `Ledger` (`drop_reported(source, items)`, `mark(source, items, outcome)` over `history.reported_*`), `PipelineContext(config, now, channel, trace, ledger, ask_model, deliver)`, `default_ask_model(now_text)` (scope-less `Session`, `chat_loop.chat(..., only_tools=)` in a thread).
- `mail.py`: `run(ctx)`, `SOURCE`, `TOOLS` (read_email, find_wilma_message, add/remove_email_label), `SYSTEM`.
- `__init__.py`: `REGISTRY = {"mail": mail.run}`, `Pipeline` type.
- → `history`, `config`, `tools.mail`. (lazy) `chat_loop`, `heartbeat.strip_sentinel`.

## vault_search.py — Obsidian search

- Public: `search(query, max_results=20, context_lines=1) -> dict`, `read(path) -> dict`.
- `search` shells `rg --json --type md --smart-case` with cwd at vault root. Honors `cfg.vault_search_excludes` (each becomes `--glob !pat`).
- `read` refuses path traversal (`is_relative_to(vault)`) and non-`.md` suffixes.
- Trims: `_LINE_CHAR_LIMIT = 300`, `_BODY_CHAR_LIMIT = 50_000`.

## wilma.py — Wilma (school portal) client

- Unofficial: speaks the web UI's cookie-session + JSON endpoints. Protocol mirrors github.com/aikarjal/wilmai (TS, MIT) — diff its changelog when Wilma breaks. Bulletin parsers are a port of its `parsers/news.ts` (MIT notice in `THIRD_PARTY_NOTICES.md`).
- Login: `GET /login` hidden fields → `SESSIONID` (or `GET /token` → `Wilma2LoginID`) → `POST /login` form → `Wilma2SID` cookie. Redirect with `loginfailed` = bad creds. Redirect page containing `mfa-formkey` → `POST /api/v1/accounts/me/mfa/otp/check` with stdlib `totp(cfg.wilma_totp_secret)`.
- Endpoints: `/api/v1/accounts/me/roles` (students; `passwd` role skipped; on 404/non-JSON falls back to `/!<n>/` links on the home page; 5xx/network errors propagate uncached), `/!<student>/messages/list[/archive|/outbox]`, `/!<student>/messages/<id>?format=json` (falls back to `/messages/<id>` HTML on 404). Bulletins: `/!<student>/news` (JSON list on newer Wilmas, else HTML: `h2.no-border` date headers + `div.well` cards, side panels `Pysyvät tiedotteet` → `pinned`, `Vanhat tiedotteet` → `archived`, titles only) and `/!<student>/news/<id>` (JSON `{title, content…}` or HTML: `#news-content` body, `a.profile-link` author, `Julkaistu D.M.[YYYY]` date). `list_news` / `get_news` → `{id, title, subtitle, author, published, content, links}`; attachments are plain links (no download).
- **One live session per account**: every login cancels the previous one, browser included. So: lazy login, module singleton `get_client()` shared across threads (`RLock` serialises requests), re-login only on 401 / redirect to `/login` / 403-with-dead-session (`_session_alive` probes roles first so a single forbidden item doesn't log the parent out).
- Rejected creds or missing TOTP secret set `_failed` → all later calls raise immediately until restart (repeated failed logins can lock the account).
- Bodies: `ContentHtml` → markdown via `markdownify`, truncated at 20 000 chars. Timestamps → ISO in Europe/Helsinki.
- `parse_timestamp` also takes `D.M.` without a year (this year unless >180 days ahead) and strips a `Julkaistu`/`Publicerad`/`Published` prefix — bulletin dates.
- `is_bulletin_notification_subject`: `Uusia tiedotteita` / `nya anslag` / `new bulletins` (search, not anchored) — those mails carry no titles, so the hint points at `list_wilma_news(since=…)`.
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
- `litellm_provider.py` — `LiteLLMProvider(model, *, aws_region=None, aws_profile=None, num_retries=2, prompt_caching=True)`. Uses `litellm.completion()` (OpenAI-shaped envelope across all backends). Module-level `litellm.drop_params=True` strips per-model unsupported kwargs. Tool-arg JSON parsed defensively; text content blocks joined, other blocks rejected. `_to_wire` → OpenAI envelope (`tool_call_id`, `arguments` as JSON string).

## frontends/

- `__init__.py` — empty.
- `common.py` — `TELEGRAM`/`DISCORD`/`ALL_FRONTENDS` `:60-62` (no CLI), `CommandSpec` + `COMMANDS` table `:66-198` (single source for `help_lines()`, `telegram_bot_commands()`, `canonicalize()`), scope helpers (`telegram_scope`, `discord_scope`, `parse_*_scope`, prefixes), arg parsers/formatters, `TOOL_COMMANDS` registry `:462`, `HELP_LINES`, async `run_remember`/`run_forget`/`run_memory`/`run_manual`/`run_heartbeat`/`run_big`/`run_status`/`run_usage`/`run_add_tool`/`run_approve`/`run_reject`/`run_diff`, `Debouncer` `:794`, `split_message`, `format_tool_status` + `ToolStatusReporter`, `format_status`/`format_usage`, `RecentWarnings`. Each `run_*` takes a `SendFn = Callable[[str], Awaitable[None]]`. `REPO_ROOT` for live_tool. `cli_command_table()` is gone.
- `telegram.py` — `start_telegram(token)`, `stop_telegram(app)`, `_TelegramChannel(app)`, `_get_telegram_session(chat_id)` (`:235`; just `Session.load(_telegram_scope_for(chat_id))` — no session dict), `_chat_lock(chat_id)` `:110`, `_run_text_turn` `:399` (`to_thread(Session.load)` → `run_turn` → reply), `/new` → `Session.reset` `:300`, `_typing(chat_id, bot)` async ctxmgr (4-second pulse), `_load_known_chats()` (reads `known_chats` rows, runs the one-time `telegram_chats.json` migration), `_remember_chat(chat_id, meta)` → `history.known_chat_touch`, `_chat_meta(update)`.
- `discord.py` — `start_discord(token)`, `stop_discord(client, task)`, `_DiscordChannel(client, heartbeat_channel_id)`, `_get_discord_session(channel_id)` (`:166`; `Session.load(_discord_scope_for(channel_id))`), `_session_lock(channel_id)` `:69`, `_run_text_turn` `:354`, `/new` → `Session.reset` `:217`, `_split_for_discord(text, limit=2000)` (paragraph→line→word→hard-cut, fence-aware), `_discord_should_respond(message, bot_user)` gating `:282`, `_strip_bot_mention(text, bot_user_id)` `:86`, `_load_known_channels()` / `_remember_channel(channel_id, meta)` / `_channel_meta(message)` (`known_chats` rows, frontend=`discord`).
