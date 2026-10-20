# frontends

Two: Telegram, Discord. Both share `chat_loop.chat()` / `run_turn()` and `frontends.common`. (The CLI REPL — `frontends/cli.py`, prompt_toolkit + rich, `/think`, `/self-edit` — was removed; `bot.py` is headless.)

## Sessions

Per-conversation history (short-term context) is **persisted in SQLite**, not held by the frontend. Long-term memory is shared via `memory.md`.

| Frontend | Session key | Scope string (`turns.session`) | Lock |
|---|---|---|---|
| Telegram | `chat_id: int` | `telegram-<chat_id>` (`_telegram_scope_for`, `telegram.py:115`) | `_chat_lock(chat_id)` (`telegram.py:110`) |
| Discord | `channel.id: int` | `discord-<channel_id>` (`_discord_scope_for`, `discord.py:82`) | `_session_lock(channel_id)` (`discord.py:69`) |

Per turn (`_run_text_turn`, `telegram.py:399` / `discord.py:354`), under the lock: `messages = await asyncio.to_thread(Session.load, scope)` → `run_turn(messages, text, lambda: scoped_chat(scope, lambda: chat(messages, on_tool_call=...)))` → reply. `Session.load` reads the rows after the latest `/new` boundary and builds a fresh system prompt; `_chat_with_provider` → `Session.commit` appends the turn's new rows only on success. There is no `_telegram_sessions` / `_discord_sessions` dict any more and no `history.record_message` — the `turns` rows **are** the session, so a restart (or a second process on the same `history.db`) continues the same conversation. `/big` loads the same way (`telegram.py:314`, `discord.py:235`).

`/new` → `await asyncio.to_thread(Session.reset, scope)` (`telegram.py:300`, `discord.py:217`): inserts a `boundary` row; nothing is deleted and older rows stay reachable via `search_conversation_history`.

The lock matters more than before: two overlapping turns on one scope would each `load` the same prefix and both `commit`, interleaving rows. Debounced Telegram turns run in timer tasks outside PTB's per-chat queue, which is why Telegram has a lock too.

## Channel registration (heartbeat fan-out)

Each frontend registers a `Channel` with `channels.register(...)` on startup:

| Class | name | Where registered |
|---|---|---|
| `_TelegramChannel(app)` | `"telegram"` | `frontends/telegram.py:450` |
| `_DiscordChannel(client, heartbeat_channel_id)` | `"discord"` | `frontends/discord.py:399` |

`channels.for_scope(scope)` (`channels.py:44`) is just `CHANNELS.get(scope.split("-", 1)[0])` — no special cases:
- `"telegram-<id>"` → Telegram channel; channel posts to that `chat_id`.
- `"discord-<id>"` → Discord channel; channel posts to **`cfg.discord_heartbeat_channel`** regardless of the scope id (one shared dest). If unset or unresolvable, `notify` raises so the heartbeat keeps the action active.
- Anything else (including a legacy `"cli"` action channel left in `heartbeat.yaml`) → `None` → the heartbeat run is marked failed and backs off.

## Active scopes (`channels.all_active_scopes()`)

- Telegram: every `chat_id` in `_known_chats`; Discord: every `channel.id` in `_known_discord_channels`. Both are in-memory mirrors of the `known_chats` table in `history.db` (`frontend`, `chat_id`, `first_seen`, `last_seen`, `meta` JSON), loaded at frontend start and touched per handled message, so fan-out survives restarts on both frontends. Debug: `sqlite3 history.db 'select * from known_chats'`.

## Slash commands

Command metadata: `frontends/common.py:74 COMMANDS` (`CommandSpec` rows; `ALL_FRONTENDS = {telegram, discord}`) drives `help_lines()`, `telegram_bot_commands()` and `canonicalize()` (aliases such as `add_tool`, `approve_force`).
Tool commands: `frontends/common.py:462 TOOL_COMMANDS` for `/train /weather /mail /search /wilma`.
Shared async runners: `run_remember`, `run_forget`, `run_memory`, `run_manual`, `run_heartbeat`, `run_big`, `run_status`, `run_usage`, `run_add_tool`, `run_approve`, `run_reject`, `run_diff`. Each takes a `SendFn = Callable[[str], Awaitable[None]]`. Both frontends delegate to them:
- Telegram: `_telegram_dispatch_command` (`telegram.py:264`) → `_dispatch_command_locked` (`telegram.py:272`, under `_chat_lock`).
- Discord: `_discord_dispatch_command` (`discord.py:190`, called under `_session_lock`, `discord.py:343`).

`/add-tool` runs `live_tool.run_add_tool_live` via `asyncio.to_thread` inside `run_add_tool` so the minutes-long `claude -p` run doesn't freeze polling (#33); the pending result is keyed by scope in `common._pending_self_change` until `/approve`, `/approve-force`, `/reject` or `/diff`. `/self-edit` and `/think` no longer exist (CLI-only, removed).

## Live tool status

- `common.format_tool_status(name, args)` -> one plain-text line (emoji by name/prefix, args clipped to 40 chars, lists >2 shown as counts).
- `common.ToolStatusReporter(loop, send, edit, limit)` is the `on_tool_call` callback. Called from the worker thread; schedules `_upsert` via `run_coroutine_threadsafe`. A single `asyncio.Lock` (FIFO) orders first `send(text)->handle` before later `edit(handle, text)`. Errors are logged and swallowed. Over `limit` (Telegram 4096 / Discord 2000) it keeps the last lines prefixed `…`.
- Telegram/Discord build one per turn via `_tool_status_for(...)`, pass it to `chat(...)` and `common.run_big(on_tool_call=...)`. The status message is left in the chat after the reply.
- `/tools [on|off]`: Telegram/Discord keep per-chat state in `_telegram_show_tools` / `_discord_show_tools` (in-memory, lost on restart — unlike the conversation itself; default `config.show_tool_calls`).
- Heartbeat runs never get a callback.

## Debounce, persistence, /status, /usage

- Non-command messages in Telegram/Discord go through `common.Debouncer` (`message_debounce_seconds`, 0 = off): each `add` restarts the per-chat timer; after quiet, buffered texts are joined with `\n` and ONE turn runs (`_run_text_turn`), replying to the latest message. The timer entry is dropped before the flush starts, so a message arriving mid-turn starts a new timer and its turn waits on the per-chat lock. Slash commands bypass it. Discord debounces only messages that passed `_discord_should_respond`.
- Locks: Discord `_session_lock(channel_id)`; Telegram `_chat_lock(chat_id)` (needed because debounced turns run in timer tasks outside PTB's serialized update queue; it also wraps `_telegram_dispatch_command`). Both guard the whole `Session.load` → turn → `commit` span.
- Persistence happens inside the chat loop (`Session.commit`), not in the frontend: every successful turn — `/big` included, since `run_big` goes through `run_turn` + `chat_via_escalation` on a loaded session — lands in `turns` as canonical messages (user text, assistant tool calls, full tool results, final reply). Nothing is written when the turn fails. The frontend only decides what to *send* (the `<think>`-stripped `clean_reply`).
- `/status` (`common.format_status`): provider/model, escalation, uptime (since `common` import), registered channels + active scope counts, heartbeat actions with `heartbeat.next_due`, last 5 WARNING+ records from `common.recent_warnings` (ring buffer handler, `bot._setup_logging` installs it on root).
- `/usage` (`common.format_usage`): today (local midnight) and last 30 days from the `usage` table, grouped by provider/model; cost `n/a` when unknown, `(partial)` when some calls unpriced.

## Telegram specifics

- Library: `python-telegram-bot >= 21`.
- HTML rendering: `telegram_format.to_html(text)` (`telegram_format.py`). Falls back to plain text on Telegram API rejection (`telegram.py:73`).
- 4096-char limit (issue #29): `_tg_reply` and `_TelegramChannel.notify` both go through `_tg_send_chunks(send, text, what=)` → `_split_for_telegram(text)` → list of `(markdown, html)` pairs. Markdown is cut with `common.split_message`, each chunk rendered to HTML; because rendering can *grow* text (tags, `&lt;` escapes) a chunk whose HTML exceeds 4096 is re-split with a 20% smaller plain budget (floor 64) until both forms fit. Each pair is sent HTML-first with per-chunk plain-text fallback. Tests: `tests/test_telegram_split.py`.
- Typing pulse: `_typing(chat_id, bot)` async ctxmgr sends `ChatAction.TYPING` every 4s (Telegram expires after ~5s).
- BotFather menu populated via `app.bot.set_my_commands(...)` (`telegram.py:454`) from `_TELEGRAM_BOT_COMMANDS = common.telegram_bot_commands()` (`telegram.py:259`).
- Known chats persisted on first message — `_remember_chat(chat_id, meta)` → `history.known_chat_touch` (`known_chats` table; the old `telegram_chats.json` is imported once and renamed `.bak`).

## Discord specifics

- Library: `discord.py >= 2.3, < 3.0`.
- Intents: `default + message_content` (privileged — must be enabled in dev portal).
- Replies sent as raw CommonMark (Discord renders natively). No HTML conversion.
- 2000-char limit handled by `_split_for_discord` → `common.split_message(text, limit)` (shared with Telegram):
  1. Cut at last `\n\n` before `limit - _FENCE_HEADROOM(32)`; else `\n`; else space; else hard cut. Headroom guarantees the fence fix-up below never has to truncate.
  2. Reopen fenced code blocks across cuts: if a chunk has odd-count fences, append `` ``` `` and prepend `` ```{lang} `` to the next.

## `_discord_should_respond` gating (`discord.py:282`)

Non-command messages — bot replies if **any**:
1. `isinstance(channel, DMChannel)` → always.
2. `message.channel.id in cfg.discord_chat_channels` → dedicated chat threads.
3. `bot_user in message.mentions` → @-mention.
4. `message.reference.resolved.author.id == bot_user.id` → reply to bot's own message.

Slash commands fire regardless of gating (parsed before gate check, `discord.py:323-333`).

## `_strip_bot_mention` (`discord.py:86`)

Removes `<@123>` and `<@!123>` (nickname form) from text before parsing. Necessary because `@bot /help` would otherwise be `<@123> /help` and fail the `text.startswith("/")` check.

## Adding a frontend

1. New module `frontends/<name>.py`. Implement: start/stop coroutines, message handler, scope helper (`<name>_scope(id)` returning `"<name>-<id>"`).
2. Per turn: take a per-conversation `asyncio.Lock`, `await asyncio.to_thread(Session.load, scope)`, `run_turn(...)` wrapped in `scoped_chat(scope, ...)`; `/new` → `asyncio.to_thread(Session.reset, scope)`. Do **not** cache `Session` objects in a module dict — the `turns` table is the store.
3. Register a `Channel` subclass with `channels.register(...)` in start (`notify` must raise on non-delivery).
4. Wire slash commands via `common.TOOL_COMMANDS` and `common.run_*` runners; add the frontend constant to `common.ALL_FRONTENDS` (or per-`CommandSpec` `frontends`).
5. Add scope helpers in `common.py` (`<name>_scope`, `parse_<name>_scope`).
6. Update `bot._async_main` to start/stop conditionally on a token (and the "at least one frontend" check).
7. Keep the channel `name` equal to the scope prefix so `channels.for_scope`'s `prefix-before-"-"` rule works unchanged.
