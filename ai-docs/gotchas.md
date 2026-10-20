# gotchas

Subtle traps. Read before editing.

## Tool registration order

`registry.TOOLS` is empty until the `tools` package is imported. `_async_main` does this at `bot.py:33` (`import tools  # noqa: F401`) **before** any frontend starts. Tests must `import tools` to populate. `tests/test_tools_registration.py` enforces.

If you add a new tool module under `tools/`, re-export it from `tools/__init__.py` so it registers on `import tools` — `/add-tool` does this automatically on approve.

## Circular imports

`tools/escalation.py` lazy-imports `chat_loop` + `providers` inside `escalate_to_big_model` (`tools/escalation.py:30`) to avoid the `chat_loop → registry → tools` cycle at module load. `heartbeat.py` lazy-imports `chat_loop` inside `run_tick` (`heartbeat.py:241`) for the same reason. Don't promote either to a top-level import.

## Test imports

Tests import directly from the defining module — `chat_loop._chat_with_provider`, `chat_loop._active_session`, `frontends.discord._split_for_discord`, etc. `bot.py` is a thin entrypoint and re-exports nothing; `import bot; bot._foo` will fail.

## `messages` mutation contract

- `chat()` and `chat_via_escalation()` take a `Session` and commit `session.history` / `session.system` only on a normal return — a provider exception leaves the session as it was. History is canonical (`messages.py`), so escalation and provider switches replay it as-is.
- `_parse_command("/")` returns `("", "")` (unknown command), never raises.
- `run_turn` appends the user message then calls `chat`. On exception it pops it to revert.
- `session.system` is **rebuilt** by `_refresh_system_prompt` before each turn AND mid-loop on memory writes — don't rely on it being stable across a turn. There is no system message inside `history`.
- Never put a provider wire dict in `session.history`; go through `append_user` / `append_exchange` / `AssistantMessage`. `append_exchange` raises `ValueError` if result ids don't match the assistant's tool calls.

## ContextVar `_active_session`

Set by `_chat_with_provider` and republished before each provider call (a point-in-time `Session` copy of system + completed exchanges, never the in-flight assistant tool call), read by `escalate_to_big_model` to splice conversation context into the escalation. **Reset on exit via `try/finally`.** If you call `_chat_with_provider` recursively (e.g. escalation), the inner call sets and resets — outer's snapshot is preserved by `ContextVar.set`/`reset` token semantics. Don't replace with a global mutable.

## Memory mutator detection is name-based

`_MEMORY_MUTATORS` (`chat_loop.py:96`) is a hard-coded frozenset of tool **names**. If you add a memory-mutating tool, add its name here, else mid-loop system rebuild won't fire.

## Tool-result shapes live in the providers

Ollama tool results carry `name`, the OpenAI envelope carries `tool_call_id`, Anthropic-native would be one `role=user` entry with `tool_result` blocks. All of that is inside each provider's `_to_wire`; the loop stores one `ToolResultMessage` per call and never sees a wire dict.

## Discord 2000-char split + fences

`_split_for_discord` reopens fenced code blocks across cuts. If you change the splitter, preserve this — splitting mid-fence breaks rendering. Tests in `tests/test_discord.py` cover.

## Discord heartbeat scope vs channel

`discord-<channel_id>` scopes all post to **`cfg.discord_heartbeat_channel`**, not back to the `channel_id` in the scope string. By design — heartbeats post to one shared place. If unset → `notify` **raises** and `run_tick` keeps the action active (it is never silently archived).

## Telegram chat-id persistence

`_known_chats` (Telegram) and `_known_discord_channels` (Discord) are in-memory mirrors of the `known_chats` table in `history.db`, loaded at frontend start and touched on every handled message (`last_seen` + merged `meta`). Deleting rows means only chats that message again after restart are eligible for heartbeat fan-out via `active_scopes()`; actions in the ledger still address their `channel` directly, so a known `telegram-<id>` action is unaffected. A DB failure in `_remember_chat` is logged and the in-memory set still updates.

## Config caching

`get_config()` is `lru_cache(1)`. Tests must call `config.reset_cache()` after env mutation (`tests/conftest.py:26`). Production never resets — config is immutable per process.

## `extra="ignore"` on Config

Pydantic silently drops unknown yaml fields. Typos in `config.yaml` won't error. Add tests for new fields rather than relying on validation to catch typos.

## Heartbeat ticks are single-flight

Never call `asyncio.create_task(heartbeat.run_tick())` directly — use `heartbeat.spawn_tick()` (keeps a strong ref, refuses while a tick runs). A second `run_tick()` while one is in flight returns `{}` immediately; the scheduler loop and `/heartbeat run` therefore never double-run or double-alert an action.

## Heartbeat `at` timestamps: naive = local

`heartbeat.parse_iso` attaches the process-local zone to a tz-naive value (`datetime.astimezone()` on a naive dt). The prompt clock (`heartbeat.format_now`) carries the UTC offset so the model usually emits aware values anyway. Don't switch naive back to UTC: the model mirrors the prompt's wall clock.

## Heartbeat sentinel match is permissive

`SENTINEL in clean` (`heartbeat.run_tick`) — model output `"HEARTBEAT_OK (nothing urgent)"` is silenced. The permissive match was deliberate. An empty reply with no sentinel is NOT an ack: it is treated as a failed run and the action stays active.

## httpx Client lifetime

`tools._HTTP` is module-level — created at import, never closed. Same for `providers/ollama._CLIENT`. Acceptable for daemon process; don't add `_HTTP.close()` cleanup unless you also rebuild on next call.

## Self-modification is single-flight

`self_change._BUSY` (a `threading.Lock`, acquired non-blocking via `busy_slot()`) guards both `run_self_change` and `live_tool.run_add_tool_live`. Both diff the working tree before/after `claude -p`; overlapping runs would see each other's new file and revert each other. `run_self_change` raises `SelfModificationBusy`; `run_add_tool_live` returns an aborted state. `common.run_add_tool` also checks `self_change.is_busy()` before the "running…" message. The per-scope `_pending_self_change` map is separate: it tracks awaiting-approval state, not running state.

## Self-change snapshot semantics

`run_self_change` snapshots dirty/untracked files **before** spawning Claude. Reject reverts only the **delta** — pre-existing dirty files are untouched. If you abort mid-run, you must clean up Claude's edits manually because the snapshot is discarded.

## CLI `_cli_messages` is a global

Set by `run_cli_repl` (`cli.py:213`). `/big` reads it via `_cli_messages_ref()`. Only one CLI session at a time — fine for a single-user REPL but not safe for concurrent CLI sessions.

## Provider history is canonical

Sessions survive a provider switch: history is `messages.Message` values and each provider serialises them itself. One caveat: Ollama-era tool-call ids look like `ollama-<hex>`; the OpenAI envelope accepts any string, so this is fine, but don't assume ids follow a provider's own format.

## MCP tools are dynamic

`mcp_client` registers into `registry.TOOLS` at startup (after `import tools`) and pops its keys on `stop()`. Tests using it must `mcp_client.stop()` in teardown. A native tool with the same `<server>_<tool>` name wins (MCP entry skipped). MCP tool funcs block their calling thread up to `call_timeout_seconds`; never call `client.call_tool` directly — the SDK `Client` lives on the `mcp` thread's loop. `mcp.Client` must be entered/exited in one task (anyio cancel scopes) → one owner task per server, stopped via its `stop` Event.
