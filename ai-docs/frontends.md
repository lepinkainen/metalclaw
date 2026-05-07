# frontends

Three: CLI, Telegram, Discord. All share `chat_loop.chat()` and `frontends.common`.

## Sessions

Each frontend keeps **per-conversation** message lists (short-term context). Long-term memory is shared via `memory.md`.

| Frontend | Session key | Storage |
|---|---|---|
| CLI | none (single REPL) | `_cli_messages` global (`cli.py:52`) |
| Telegram | `chat_id: int` | `_telegram_sessions: dict[int, list[dict]]` (`telegram.py:28`) |
| Discord | `channel.id: int` | `_discord_sessions: dict[int, list[dict]]` (`discord.py:23`) |

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
- `"discord-<id>"` → Discord channel; channel posts to **`cfg.discord_heartbeat_channel`** regardless of the scope id (one shared dest). If unset, drops with warning.

## Active scopes (for `discover_scopes` fallback / `all_active_scopes()`)

- CLI: always `("cli",)`.
- Telegram: every `chat_id` in `_known_chats` (persisted to `$XDG_DATA_HOME/metalclaw/telegram_chats.json`).
- Discord: every `channel.id` in `_known_discord_channels` (in-memory only; populated as messages arrive).

## Slash commands

Shared registry: `frontends/common.py:230 TOOL_COMMANDS` for `/train /weather /mail /search`.
Shared async runners: `run_remember`, `run_forget`, `run_memory`, `run_heartbeat`, `run_big`. Each takes a `SendFn = Callable[[str], Awaitable[None]]`.

CLI does **not** delegate `/remember /forget /memory /heartbeat /big` to `common.run_*` — it has Rich-styled sync handlers (`cli.py:142-156`). The other frontends do delegate:
- Telegram: `_telegram_dispatch_command` (`telegram.py:154`).
- Discord: `_discord_dispatch_command` (`discord.py:140`).

## CLI specifics

- `prompt_toolkit.PromptSession` with `SQLiteHistory(session_id)` for up-arrow recall.
- `rich.Console(highlight=False)`. Replies rendered via `Markdown(text)`.
- `_show_thinking` global toggled by `/think`; only CLI ever displays `<think>...</think>` content (`cli.py:271`).
- `/add-tool` and `/self-edit` shell out to `self_change.run_self_change` — interactive approve/reject loop is CLI-only.
- `console.status("[dim]thinking…[/dim]", spinner="dots")` while waiting on `chat()`.
- Model dispatch via `await loop.run_in_executor(None, lambda: chat(messages, on_tool_call=_cli_tool_log))` (`cli.py:262`).

## Telegram specifics

- Library: `python-telegram-bot >= 21`.
- HTML rendering: `telegram_format.to_html(text)` (`telegram_format.py`). Falls back to plain text on Telegram API rejection (`telegram.py:73`).
- Typing pulse: `_typing(chat_id, bot)` async ctxmgr sends `ChatAction.TYPING` every 4s (Telegram expires after ~5s).
- BotFather menu populated via `app.bot.set_my_commands(...)` from `_TELEGRAM_BOT_COMMANDS` (`telegram.py:137`).
- Known chats persisted on first message — `_remember_chat(chat_id)` writes JSON.

## Discord specifics

- Library: `discord.py >= 2.3, < 3.0`.
- Intents: `default + message_content` (privileged — must be enabled in dev portal).
- Replies sent as raw CommonMark (Discord renders natively). No HTML conversion.
- 2000-char limit handled by `_split_for_discord` (`discord.py:41`):
  1. Cut at last `\n\n` before limit; else `\n`; else space; else hard cut.
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
