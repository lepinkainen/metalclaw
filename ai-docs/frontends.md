# frontends

Three: CLI, Telegram, Discord. All share `chat_loop.chat()` and `frontends.common`.

## Sessions

Each frontend keeps **per-conversation** message lists (short-term context). Long-term memory is shared via `memory.md`.

| Frontend | Session key | Storage |
|---|---|---|
| CLI | none (single REPL) | `_cli_messages: Session | None` global (`cli.py`) |
| Telegram | `chat_id: int` | `_telegram_sessions: dict[int, Session]` (`telegram.py`) |
| Discord | `channel.id: int` | `_discord_sessions: dict[int, Session]` (`discord.py`) |

Each value is a `chat_loop.Session` (system prompt + canonical `messages.py` history). `/new` pops the entry; nothing else to release.

`/new` clears the session (`telegram.py:163`, `discord.py:149`). CLI has no `/new` — exit and restart.

## Channel registration (heartbeat fan-out)

Each frontend registers a `Channel` with `channels.register(...)` on startup:

| Class | name | Where registered |
|---|---|---|
| `_CLIChannel` | `"cli"` | `frontends/cli.py:200` |
| `_TelegramChannel(app)` | `"telegram"` | `frontends/telegram.py:248` |
| `_DiscordChannel(client, heartbeat_channel_id)` | `"discord"` | `frontends/discord.py:273` |

`channels.for_scope(scope)` resolves:
- `"cli"` → CLI channel.
- `"telegram-<id>"` → Telegram channel; channel posts to that `chat_id`.
- `"discord-<id>"` → Discord channel; channel posts to **`cfg.discord_heartbeat_channel`** regardless of the scope id (one shared dest). If unset or unresolvable, `notify` raises so the heartbeat keeps the action active.

## Active scopes (`channels.all_active_scopes()`)

- CLI: always `("cli",)`.
- Telegram: every `chat_id` in `_known_chats`; Discord: every `channel.id` in `_known_discord_channels`. Both are in-memory mirrors of the `known_chats` table in `history.db` (`frontend`, `chat_id`, `first_seen`, `last_seen`, `meta` JSON), loaded at frontend start and touched per handled message, so fan-out survives restarts on both frontends. Debug: `sqlite3 history.db 'select * from known_chats'`.

## Slash commands

Shared registry: `frontends/common.py:230 TOOL_COMMANDS` for `/train /weather /mail /search /wilma`.
Shared async runners: `run_remember`, `run_forget`, `run_memory`, `run_heartbeat`, `run_big`. Each takes a `SendFn = Callable[[str], Awaitable[None]]`.

CLI does **not** delegate `/remember /forget /memory /heartbeat /big` to `common.run_*` — it has Rich-styled sync handlers. CLI tool slash commands (`_make_tool_handler`) and `/self-edit` are `async` and run their blocking body via `asyncio.to_thread` so a slow HTTP call or the minutes-long `claude -p` run doesn't freeze Telegram/Discord polling (#33). The other frontends do delegate:
- Telegram: `_telegram_dispatch_command` (`telegram.py:154`).
- Discord: `_discord_dispatch_command` (`discord.py:140`).

## CLI specifics

- `prompt_toolkit.PromptSession` with `SQLiteHistory(session_id)` for up-arrow recall.
- `rich.Console(highlight=False)`. Replies rendered via `Markdown(text)`.
- `_show_thinking` global toggled by `/think`; only CLI ever displays `<think>...</think>` content (`cli.py:271`).
- `/add-tool` and `/self-edit` shell out to `self_change.run_self_change` — interactive approve/reject loop is CLI-only.
- `console.status("[dim]thinking…[/dim]", spinner="dots")` while waiting on `chat()`.
- Model dispatch via `await loop.run_in_executor(None, lambda: chat(messages, on_tool_call=_cli_tool_log))` (`cli.py:262`).

## Live tool status

- `common.format_tool_status(name, args)` -> one plain-text line (emoji by name/prefix, args clipped to 40 chars, lists >2 shown as counts).
- `common.ToolStatusReporter(loop, send, edit, limit)` is the `on_tool_call` callback. Called from the worker thread; schedules `_upsert` via `run_coroutine_threadsafe`. A single `asyncio.Lock` (FIFO) orders first `send(text)->handle` before later `edit(handle, text)`. Errors are logged and swallowed. Over `limit` (Telegram 4096 / Discord 2000) it keeps the last lines prefixed `…`.
- Telegram/Discord build one per turn via `_tool_status_for(...)`, pass it to `chat(...)` and `common.run_big(on_tool_call=...)`. The status message is left in the chat after the reply.
- `/tools [on|off]` (all frontends): Telegram/Discord keep per-chat state in `_telegram_show_tools` / `_discord_show_tools` (in-memory; default `config.show_tool_calls`). CLI `_show_tools` is initialised from config; `/tools` toggles, result lines shown only with `/think` on.
- Heartbeat runs never get a callback.

## Debounce, persistence, /status, /usage

- Non-command messages in Telegram/Discord go through `common.Debouncer` (`message_debounce_seconds`, 0 = off): each `add` restarts the per-chat timer; after quiet, buffered texts are joined with `\n` and ONE turn runs (`_run_text_turn`), replying to the latest message. The timer entry is dropped before the flush starts, so a message arriving mid-turn starts a new timer and its turn waits on the per-chat lock. Slash commands bypass it. Discord debounces only messages that passed `_discord_should_respond`.
- Locks: Discord `_session_lock(channel_id)`; Telegram `_chat_lock(chat_id)` (needed because debounced turns run in timer tasks outside PTB's serialized update queue; it also wraps `_telegram_dispatch_command`).
- Each turn records the user text and clean reply to `history.db` via `history.record_message(scope, role, text)` (not `/big` turns).
- `/status` (`common.format_status`): provider/model, escalation, uptime (since `common` import), registered channels + active scope counts, heartbeat actions with `heartbeat.next_due`, last 5 WARNING+ records from `common.recent_warnings` (ring buffer handler, `bot._setup_logging` installs it on root).
- `/usage` (`common.format_usage`): today (local midnight) and last 30 days from the `usage` table, grouped by provider/model; cost `n/a` when unknown, `(partial)` when some calls unpriced.

## Telegram specifics

- Library: `python-telegram-bot >= 21`.
- HTML rendering: `telegram_format.to_html(text)` (`telegram_format.py`). Falls back to plain text on Telegram API rejection (`telegram.py:73`).
- 4096-char limit (issue #29): `_tg_reply` and `_TelegramChannel.notify` both go through `_tg_send_chunks(send, text, what=)` → `_split_for_telegram(text)` → list of `(markdown, html)` pairs. Markdown is cut with `common.split_message`, each chunk rendered to HTML; because rendering can *grow* text (tags, `&lt;` escapes) a chunk whose HTML exceeds 4096 is re-split with a 20% smaller plain budget (floor 64) until both forms fit. Each pair is sent HTML-first with per-chunk plain-text fallback. Tests: `tests/test_telegram_split.py`.
- Typing pulse: `_typing(chat_id, bot)` async ctxmgr sends `ChatAction.TYPING` every 4s (Telegram expires after ~5s).
- BotFather menu populated via `app.bot.set_my_commands(...)` from `_TELEGRAM_BOT_COMMANDS` (`telegram.py:137`).
- Known chats persisted on first message — `_remember_chat(chat_id)` writes JSON.

## Discord specifics

- Library: `discord.py >= 2.3, < 3.0`.
- Intents: `default + message_content` (privileged — must be enabled in dev portal).
- Replies sent as raw CommonMark (Discord renders natively). No HTML conversion.
- 2000-char limit handled by `_split_for_discord` → `common.split_message(text, limit)` (shared with Telegram):
  1. Cut at last `\n\n` before `limit - _FENCE_HEADROOM(32)`; else `\n`; else space; else hard cut. Headroom guarantees the fence fix-up below never has to truncate.
  2. Reopen fenced code blocks across cuts: if a chunk has odd-count fences, append `` ``` `` and prepend `` ```{lang} `` to the next.

## `_discord_should_respond` gating (`discord.py:194`)

Non-command messages — bot replies if **any**:
1. `isinstance(channel, DMChannel)` → always.
2. `message.channel.id in cfg.discord_chat_channels` → dedicated chat threads.
3. `bot_user in message.mentions` → @-mention.
4. `message.reference.resolved.author.id == bot_user.id` → reply to bot's own message.

Slash commands fire regardless of gating (parsed before gate check, `discord.py:227-230`).

## `_strip_bot_mention` (`discord.py:35`)

Removes `<@123>` and `<@!123>` (nickname form) from text before parsing. Necessary because `@bot /help` would otherwise be `<@123> /help` and fail the `text.startswith("/")` check.

## Adding a frontend

1. New module `frontends/<name>.py`. Implement: start/stop coroutines, message handler, scope helper (`<name>_scope(id)` returning `"<name>-<id>"`).
2. Register a `Channel` subclass with `channels.register(...)` in start.
3. Wire slash commands via `common.TOOL_COMMANDS` and `common.run_*` runners.
4. Add scope helpers in `common.py` (`<name>_scope`, `parse_<name>_scope`).
5. Update `bot._async_main` to start/stop conditionally on a token.
6. Update `channels.for_scope` if name doesn't match the simple `prefix-` rule.
