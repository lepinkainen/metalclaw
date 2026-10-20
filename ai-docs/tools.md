# tools

## Registration

`@tool(description=..., args=PydanticModel | None)` decorator (`registry.py:38`):
1. `args=None` → `_EMPTY_PARAMETERS` (zero-arg).
2. Else `_schema_from_model(args)` inlines every `$ref` into the referencing node (`_inline_refs`, so nested pydantic models become self-contained `object` specs — no `$defs`), then recursively (`_clean_spec`) strips `title`, collapses `Optional` `anyOf` to a single non-null type, and drops `default: None` keys.
3. Stores `Tool(func, schema={"type":"function","function":{"name","description","parameters"}})` in `TOOLS[func.__name__]`.

The function `__name__` is the registry key — also what providers see as the tool name. Don't shadow.

## Schema shape (consumed by providers)

```python
{
    "type": "function",
    "function": {
        "name": "weather",
        "description": "...",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "..."},
            },
            "required": ["location"],
        },
    },
}
```

Ollama and litellm consume this directly (litellm normalises to per-backend formats internally — Anthropic's `input_schema`, etc.).

## Catalog

| Name | File:line | Args | Returns | Notes |
|---|---|---|---|---|
| `roll_die` | `tools.py:18` | `sides: int` | `str` | RNG. |
| `weather` | `tools.py:94` | `location: str` | `dict` | Geocode via Nominatim → MET Norway forecast. Today + tomorrow summaries. |
| `train_departures` | `tools.py:175` | `station: str, line?: str, count: int=5` | `dict` | Digitraffic Finnish rail. Filters by commuter line letter. |
| `list_emails` | `tools.py:346` | `mailbox="inbox", unread_only=False, from_search?, limit?` | `dict` | Fastmail JMAP. `mailbox="all"` sweeps everywhere except trash/junk/drafts/sent. |
| `read_email` | `tools.py:494` | `email_id: str` | `dict` | Body + attachments. Truncates at 20000 chars. HTML→markdown via `markdownify`. |
| `check_new_mail` | `tools/mail.py` | — | `dict` | Unread-anywhere or flagged-Inbox emails (limit 50) minus SQLite ledger (`mail_reported.sqlite3`). Stores ids in `_last_candidates`. |
| `mark_emails_reported` | `tools/mail.py` | `ids: list[str]` | `dict` | Records ids from last `check_new_mail` into ledger; others → `rejected`. Never mutates mailbox. |
| `list_email_labels` | `tools/mail.py` | — | `list[dict]` | All mailboxes as `{path, role, total, unread}` sorted by path. Read-only. |
| `add_email_label` | `tools/mail.py` | `email_ids: list[str]` (1-50), `label: str` | `dict` | Label must start with `bot/` (enforced by `_validate_bot_label`); missing `bot/...` mailboxes are created. `Email/set` patches `mailboxIds/<id>: true`. Returns `{label, updated, failed}`. |
| `remove_email_label` | `tools/mail.py` | same | `dict` | Same `bot/` restriction; errors if label missing (never creates). Patches `mailboxIds/<id>: null`. |
| `list_wilma_messages` | `tools/wilma.py` | `student?, folder="inbox", unread_only=False, limit=20` | `dict` | Per child `{student, student_name, messages[]}`; newest first. `student` = name substring or number. |
| `read_wilma_message` | `tools/wilma.py` | `message_id: int, student?` | `dict` | Full body (markdown), recipients, replies. Without `student` tries each child. |
| `find_wilma_message` | `tools/wilma.py` | `subject, sent_at?, student?` | `dict` | Resolves a "Viesti Wilmasta" notification email to the real message. `{status: found/ambiguous/not_found}`; dedups the same message across children (`students` lists them). `check_new_mail` adds `hint` to such emails when `wilma_url` is set. |
| `set_user_preference` | `tools.py:618` | `key, value` | `dict` | → `memory.set_preference`. |
| `add_user_fact` | `tools.py:635` | `text` | `dict` | → `memory.add_fact`. |
| `add_user_instruction` | `tools.py:658` | `text` | `dict` | → `memory.add_instruction`. Behavioural rule. |
| `forget_user_memory` | `tools.py:678` | `matcher` | `dict` | → `memory.forget`. Returns `{status: removed/ambiguous/not_found, ...}`. |
| `get_user_memory` | `tools.py:700` | none | `{markdown: str}` | Full memory file. |
| `append_to_inbox` | `tools/inbox.py` | `text` | `dict` | Appends `- YYYY-MM-DD HH:MM — text` to `<vault_path>/INBOX.md` (fixed name, no path arg; created if missing; `fcntl.flock` on the append fd). Errors if `vault_path` is not a dir. |
| `search_conversation_history` | `tools/conversation_history.py` | `query, limit=10` | `dict` | `history.search_messages`: case-insensitive LIKE over user text + assistant replies in the `turns` table (tool calls/results excluded) UNION the legacy `messages` table, newest first; content trimmed to 500 chars. `surface` = scope string, or `"cli"` for legacy bare-timestamp rows (`:44`). |
| `search_vault` | `tools.py:725` | `query, max_results=20, context_lines=1` | `dict` | ripgrep wrapper via `vault_search.search`. |
| `read_note` | `tools.py:748` | `path` | `dict` | Vault note read via `vault_search.read`. |
| `escalate_to_big_model` | `tools.py:768` | `query, reason` | `dict` | Calls big model with current session snapshot. Returns `{status, model, reason, reply}`. Disabled if `cfg.escalation_enabled=False`. |

## MCP tools (`mcp_client.py`)

Not `@tool`-decorated: `mcp_client._register` writes `registry.Tool(func=<closure>, schema=<mapped>, args_model=None)` directly into `TOOLS` at startup (after `import tools`, so native names win on collision — skipped with a warning).
- Name `<server>_<remote>` sanitised to `[A-Za-z0-9_-]`, clipped to 64. Description `[<server>] <desc>` (≤1024). `input_schema` passed through minus `$schema`/`title`; `$ref`/`x-mcp-header` etc. untouched.
- `args_model=None` → no local validation; the server's `is_error` text comes back as `Error: ...`.
- Result: `{"source": "mcp:<server>", "untrusted": true, "result": structured_content (unwrapped if just {"result": x}) or joined text}`; image/audio/blob → `[... N bytes omitted]` placeholders. Over 20000 chars (`_RESULT_CHAR_LIMIT`) → clipped JSON/text string ending `…[truncated N chars]`.
- Timeout (`call_timeout_seconds`) / transport failure → `Error: mcp <server>.<tool> ...` string; failure stored in `status()` `error`.
- Listed automatically by `read_manual` / `/manual` (iterates `TOOLS`). `/status` shows per-server up/down + tool names.
- Current real server: GitHub remote (read-only), `include: [get_me, search_repositories, list_issues, issue_read, actions_list]` → `github_get_me` etc. Remote tool names are consolidated with a `method` arg (`issue_read` method `get`/`get_comments`; `actions_list` method `list_workflow_runs`).

## `_MEMORY_MUTATORS` set

`chat_loop.py:227` — must include any tool that mutates `memory.md` or the heartbeat ledger:
```python
{
    "set_user_preference",
    "add_user_fact",
    "add_user_instruction",
    "forget_user_memory",
    "create_heartbeat_action",
    "cancel_heartbeat_action",
}
```
Adding a new memory-mutating tool? Update this set, else mid-loop system rebuild won't fire.

## Tool-result format

`_run_tool` (`chat_loop.py:274`):
- Catches all `Exception` and returns `f"Error: {e}"` as the result.
- Otherwise returns the function's return value (any JSON-serializable shape).

`_chat_with_provider` then serializes via `json.dumps(result, ensure_ascii=False)` into a `ToolResultMessage(call_id, name, content)`; the provider's `_to_wire` turns that into its envelope on the next call. That same `content` string is what `Session.commit` persists to the `turns` table, **uncut** — keep tool outputs bounded (see `gotchas.md#turns-table-growth`).

## Slash command wiring (`frontends/common.py`)

Each tool that wants a slash command needs:
1. **Parser**: `parse_<name>_args(args: str) -> dict` (uses `argparse` via `_ArgParser` that raises `ValueError`, or manual parse for free-form).
2. **Formatter**: `format_<name>_result(result: dict) -> str` — markdown-friendly for Discord; HTML conversion happens automatically for Telegram.
3. **Registry entry**: `TOOL_COMMANDS["<slash>"] = ("<tool_registry_name>", parser, formatter)` (`frontends/common.py:462`).
4. **Command spec**: a `CommandSpec` row in `common.COMMANDS` (`frontends/common.py:74`) so `/help`, Telegram's BotFather menu and `canonicalize()` know it.

Currently: `train`, `weather`, `mail`, `search`, `wilma`. Both frontends dispatch via the `canon in common.TOOL_COMMANDS` branch (`frontends/telegram.py:272 _dispatch_command_locked`, `frontends/discord.py:190 _discord_dispatch_command`), running the tool in an executor.

## Adding a new tool

1. Define pydantic args model (or skip for zero-arg) in a new `tools/<domain>.py`; re-export from `tools/__init__.py` (or let `/add-tool` do both).
2. Define function decorated with `@tool(description="...", args=Model)`. Keep description model-friendly — it's the prompt that decides when the tool fires. Keep results bounded: they are persisted verbatim in `turns`.
3. If memory-mutating: add name to `_MEMORY_MUTATORS` in `chat_loop.py`.
4. Optional slash command: add parser + formatter + `TOOL_COMMANDS` entry + `CommandSpec` in `frontends/common.py`; the Telegram bot menu and `/help` derive from `COMMANDS` automatically.
5. Test: `tests/test_tools_registration.py` already verifies registration.
6. Test: `tests/test_routing.py` for slash dispatch.

## httpx client lifetime

`_HTTP = httpx.Client(headers={...}, timeout=15.0)` at module level in `tools/_http.py`. Shared across all tool modules. Connection pool persists for the lifetime of the process. Don't add a `close()` — recreating per-call doubles latency.

## Fastmail session caching

`_FM_SESSION` and `_FM_MAILBOXES` are lazily populated module-level globals. Reset them by restarting the bot (`_fm_invalidate_mailboxes()` clears the mailbox cache after label creation). Label writes need a Fastmail API token with mail write access; read-only tokens fail with forbidden/accountReadOnly. If Fastmail rotates the token, restart.

## Escalation tool details

`escalate_to_big_model(query, reason)`:
1. Disabled if `cfg.escalation_enabled=False` → returns `{status: disabled}`.
2. Reads `chat_loop._active_session.get()` (ContextVar, a scope-less `Session` copy): system + completed exchanges of the current turn, including earlier tool results from this turn, excluding the in-flight escalate call.
3. `sub = snapshot.snapshot(); sub.append_user(f"[escalation: {reason}] {query}")`.
4. Calls `chat_loop._chat_with_provider(big, sub, exclude_tools={"escalate_to_big_model"})` — recursion-prevented; `sub.commit` is in-memory (no scope).
5. Returns `{"status":"ok","model":"<provider>:<model>","reason","reply"}`.

The local model sees `reply` as a tool result and produces user-facing text. The escalation sub-conversation is **not** appended to the active session history (nor to `turns`) — only the tool result and the synthesized response are.
