"""Telegram frontend: per-chat sessions, slash commands, message routing."""

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path
from typing import Any

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import channels
import history
import telegram_format
from chat_loop import (
    Session,
    _parse_command,
    build_system_prompt,
    chat,
    format_now,
    run_turn,
    scoped_chat,
)
from config import get_config, xdg_data_dir
from frontends import common
from registry import TOOLS

log = logging.getLogger(__name__)

_telegram_sessions: dict[int, Session] = {}
_telegram_chat_locks: dict[int, asyncio.Lock] = {}
# In-memory mirror of the ``known_chats`` rows for frontend="telegram"
# (history.db). Loaded in ``start_telegram``; every message touches the row.
_known_chats: set[int] = set()
_FRONTEND = "telegram"
_LEGACY_CHATS_FILENAME = "telegram_chats.json"


def _legacy_chats_path() -> Path:
    return xdg_data_dir() / _LEGACY_CHATS_FILENAME


def _migrate_legacy_chats_file() -> None:
    """One-time import of the pre-SQLite ``telegram_chats.json`` list, then ``.bak``."""
    path = _legacy_chats_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        log.exception("telegram: legacy known-chats file unreadable, leaving %s", path)
        return
    ids = [str(int(x)) for x in data] if isinstance(data, list) else []
    history.known_chats_import(
        _FRONTEND, ids, {"migrated_from": _LEGACY_CHATS_FILENAME}
    )
    backup = path.with_suffix(path.suffix + ".bak")
    try:
        os.replace(path, backup)
    except OSError as e:
        log.warning("telegram: could not rename %s -> %s: %s", path, backup, e)
    log.info("telegram: migrated %d known chat(s) from %s", len(ids), path.name)


def _load_known_chats() -> set[int]:
    _migrate_legacy_chats_file()
    out: set[int] = set()
    for cid in history.known_chat_ids(_FRONTEND):
        try:
            out.add(int(cid))
        except ValueError:
            log.warning("telegram: ignoring non-integer known chat id %r", cid)
    return out


def _chat_meta(update: Update) -> dict:
    chat = update.effective_chat
    user = update.effective_user
    return {
        "type": getattr(chat, "type", None),
        "title": getattr(chat, "title", None),
        "username": getattr(user, "username", None),
        "user_id": getattr(user, "id", None),
    }


def _remember_chat(chat_id: int, meta: dict | None = None) -> None:
    try:
        created = history.known_chat_touch(_FRONTEND, str(chat_id), meta)
    except Exception:
        log.exception("telegram: could not persist known chat %d", chat_id)
        created = chat_id not in _known_chats
    if chat_id not in _known_chats:
        _known_chats.add(chat_id)
        log.info(
            "telegram: new chat remembered chat_id=%d created=%s (known=%d)",
            chat_id,
            created,
            len(_known_chats),
        )


def _chat_lock(chat_id: int) -> asyncio.Lock:
    """Serialises turns/commands per chat; debounced turns run outside PTB's update queue."""
    return _telegram_chat_locks.setdefault(chat_id, asyncio.Lock())


def _telegram_scope_for(chat_id: int) -> str:
    return common.telegram_scope(chat_id)


def known_chat_count() -> int:
    return len(_known_chats)


_TELEGRAM_MAX_MESSAGE = 4096


def _split_for_telegram(
    text: str, limit: int = _TELEGRAM_MAX_MESSAGE, *, plain_limit: int | None = None
) -> list[tuple[str, str]]:
    """Split Markdown into Telegram-sized ``(markdown, html)`` pairs.

    Telegram caps a message at 4096 chars *after* HTML rendering, and
    rendering can grow the text (tags, entity escapes). Each Markdown chunk
    is cut to `plain_limit` then rendered; if the HTML overflows `limit` the
    chunk is re-split with a 20% smaller plain budget until both forms fit —
    so the plain-text fallback is always sendable too.
    """
    budget = limit if plain_limit is None else plain_limit
    pairs: list[tuple[str, str]] = []
    for chunk in common.split_message(text, budget):
        html_text = telegram_format.to_html(chunk)
        if len(html_text) > limit and budget > 64:
            pairs.extend(
                _split_for_telegram(chunk, limit, plain_limit=int(budget * 0.8))
            )
        else:
            pairs.append((chunk, html_text))
    return pairs


async def _tg_send_chunks(
    send: Callable[..., Awaitable[Any]], text: str, *, what: str
) -> None:
    """Send `text` as one or more Telegram messages via `send(text, **kw)`.

    Tries HTML first per chunk and falls back to plain text when Telegram
    rejects the markup. Any other failure propagates so callers (heartbeat
    notify, command dispatch) see it.
    """
    pairs = _split_for_telegram(text)
    log.info("telegram: %s %d chars in %d message(s)", what, len(text), len(pairs))
    for plain, html_text in pairs:
        try:
            await send(html_text, parse_mode="HTML")
        except Exception:
            log.warning(
                "telegram: HTML %s failed, falling back to plain text",
                what,
                exc_info=True,
            )
            await send(plain)


async def _tg_reply(update: Update, text: str) -> None:
    """Send a Telegram reply, rendering CommonMark as Telegram HTML."""
    assert update.message is not None
    await _tg_send_chunks(update.message.reply_text, text, what="reply")


def _send_for(update: Update) -> common.SendFn:
    async def _send(text: str) -> None:
        await _tg_reply(update, text)

    return _send


@asynccontextmanager
async def _typing(chat_id: int, bot):
    """Show 'typing…' in the chat until the context exits.

    Telegram chat actions expire after ~5 seconds, so we refresh on a 4-second
    cadence. Errors are swallowed — this is best-effort UX.
    """

    async def _pulse() -> None:
        while True:
            try:
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            except Exception:
                log.debug("telegram: typing pulse failed", exc_info=True)
            await asyncio.sleep(4)

    task = asyncio.create_task(_pulse())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError, Exception:
            log.debug("telegram: background task ended with error", exc_info=True)


class _TelegramChannel:
    name = "telegram"

    def __init__(self, app: Application) -> None:
        self._app = app

    async def notify(self, scope: str, text: str) -> None:
        chat_id = common.parse_telegram_scope(scope)
        if chat_id is None:
            # raise so heartbeat keeps the action active instead of archiving it
            raise ValueError(f"telegram: notify with unparseable scope {scope!r}")
        bot = self._app.bot

        async def _send(body: str, **kw: Any) -> Any:
            return await bot.send_message(chat_id=chat_id, text=body, **kw)

        await _tg_send_chunks(_send, text, what=f"notify chat_id={chat_id}")

    def active_scopes(self) -> Iterable[str]:
        return tuple(common.telegram_scope(cid) for cid in _known_chats)


def _get_telegram_session(chat_id: int) -> Session:
    if chat_id not in _telegram_sessions:
        log.info("telegram: new session chat_id=%d", chat_id)
        now = format_now()
        _telegram_sessions[chat_id] = Session(system=build_system_prompt(now))
    return _telegram_sessions[chat_id]


# Per-chat live tool-status toggle; absent -> config.show_tool_calls.
_telegram_show_tools: dict[int, bool] = {}


def _tool_status_for(chat_id: int, bot) -> common.ToolStatusReporter | None:
    if not _telegram_show_tools.get(chat_id, get_config().show_tool_calls):
        return None

    async def _send(text: str):
        return await bot.send_message(chat_id=chat_id, text=text)

    async def _edit(msg, text: str):
        await bot.edit_message_text(text, chat_id=chat_id, message_id=msg.message_id)

    return common.ToolStatusReporter(
        asyncio.get_running_loop(), _send, _edit, common.TELEGRAM_STATUS_LIMIT
    )


_TELEGRAM_BOT_COMMANDS: list[tuple[str, str]] = common.telegram_bot_commands()

_TELEGRAM_HELP_TEXT = "\n".join(common.HELP_LINES)


async def _telegram_dispatch_command(
    update: Update, cmd: str, args: str, bot=None
) -> None:
    assert update.effective_chat is not None
    async with _chat_lock(update.effective_chat.id):
        await _dispatch_command_locked(update, cmd, args, bot)


async def _dispatch_command_locked(
    update: Update, cmd: str, args: str, bot=None
) -> None:
    assert update.effective_chat is not None
    chat_id = update.effective_chat.id
    scope = _telegram_scope_for(chat_id)
    send = _send_for(update)
    canon = common.canonicalize(cmd) or cmd
    log.info(
        "telegram: command /%s (canon=%s) chat_id=%d args=%d chars",
        cmd,
        canon,
        chat_id,
        len(args),
    )

    if canon == "help":
        await _tg_reply(update, _TELEGRAM_HELP_TEXT)
    elif canon == "tools":
        cur = _telegram_show_tools.get(chat_id, get_config().show_tool_calls)
        new, reply = common.parse_tools_arg(cur, args)
        _telegram_show_tools[chat_id] = new
        await _tg_reply(update, reply)
    elif canon == "status":
        await common.run_status(send)
    elif canon == "usage":
        await common.run_usage(send)
    elif canon == "new":
        old = _telegram_sessions.pop(chat_id, None)
        log.info(
            "telegram: session reset chat_id=%d (had_session=%s)",
            chat_id,
            old is not None,
        )
        await _tg_reply(update, "Conversation reset.")
    elif canon == "remember":
        await common.run_remember(send, args)
    elif canon == "forget":
        await common.run_forget(send, args)
    elif canon == "memory":
        await common.run_memory(send)
    elif canon == "manual":
        await common.run_manual(send, args)
    elif canon == "heartbeat":
        await common.run_heartbeat(send, scope, args.strip())
    elif canon == "big":
        typing_ctx = _typing(chat_id, bot) if bot is not None else nullcontext()
        await common.run_big(
            send,
            typing_ctx,
            _get_telegram_session(chat_id),
            args.strip(),
            scope=scope,
            on_tool_call=_tool_status_for(chat_id, bot) if bot is not None else None,
        )
    elif canon == "add-tool":
        await common.run_add_tool(send, args, scope)
    elif canon == "approve":
        await common.run_approve(send, scope)
    elif canon == "approve-force":
        await common.run_approve(send, scope, force=True)
    elif canon == "reject":
        await common.run_reject(send, scope)
    elif canon == "diff":
        await common.run_diff(send, scope)
    elif cmd in common.TOOL_COMMANDS:
        tool_name, parser, formatter = common.TOOL_COMMANDS[cmd]
        try:
            params = parser(args)
        except ValueError as e:
            log.info("telegram: /%s bad args: %s", cmd, e)
            await _tg_reply(update, str(e))
            return
        log.info(
            "telegram: tool command /%s -> %s params=%.200s", cmd, tool_name, params
        )
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
            log.warning("telegram: tool %s unavailable", tool_name)
            await _tg_reply(update, f"{tool_name} tool unavailable")
            return
        loop = asyncio.get_running_loop()
        try:
            if bot is not None:
                async with _typing(chat_id, bot):
                    result = await loop.run_in_executor(
                        None, lambda: tool_obj.func(**params)
                    )
            else:
                result = await loop.run_in_executor(
                    None, lambda: tool_obj.func(**params)
                )
        except Exception as e:
            log.exception("telegram: tool command /%s failed", cmd)
            await _tg_reply(update, f"Error: {e}")
            return
        await _tg_reply(update, formatter(result))
    else:
        log.info("telegram: unknown command /%s", cmd)
        await _tg_reply(update, f"Unknown command: /{cmd}  (try /help)")


async def _telegram_handle_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    assert update.message is not None and update.message.text is not None
    assert update.effective_chat is not None
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    log.info(
        "telegram: message chat_id=%d user=%s len=%d",
        chat_id,
        getattr(user, "username", None) or getattr(user, "id", None),
        len(text),
    )
    log.debug("telegram: message text: %.500s", text)
    _remember_chat(chat_id, _chat_meta(update))

    parsed = _parse_command(text)
    if parsed is not None:
        cmd, args = parsed
        await _telegram_dispatch_command(update, cmd, args, context.bot)
        return

    delay = get_config().message_debounce_seconds
    if delay > 0:
        _debouncer.add(chat_id, text, (update, context), delay)
        return
    await _run_text_turn(chat_id, text, (update, context))


async def _run_text_turn(chat_id: int, text: str, ctx: tuple) -> None:
    """One chat turn for ``text``; replies to the latest message's ``update``."""
    update, context = ctx
    async with _chat_lock(chat_id):
        messages = _get_telegram_session(chat_id)
        scope = _telegram_scope_for(chat_id)
        history.record_message(scope, "user", text)
        on_tool = _tool_status_for(chat_id, context.bot)
        try:
            async with _typing(chat_id, context.bot):
                _, _, clean_reply = await run_turn(
                    messages,
                    text,
                    lambda: scoped_chat(
                        scope, lambda: chat(messages, on_tool_call=on_tool)
                    ),
                )
        except Exception as e:
            log.exception("telegram: turn failed chat_id=%d", chat_id)
            await _tg_reply(update, f"Error: {e}")
            return
        if on_tool is not None:
            await on_tool.drain()
        history.record_message(scope, "assistant", clean_reply)
        log.info("telegram: reply chat_id=%d len=%d", chat_id, len(clean_reply))
        await _tg_reply(update, clean_reply)


_debouncer = common.Debouncer(_run_text_turn)


def _make_telegram_cmd_handler(cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _telegram_dispatch_command(
            update, cmd, " ".join(context.args or []), context.bot
        )

    return handler


async def start_telegram(token: str) -> Application:
    global _known_chats
    _known_chats = _load_known_chats()

    # PTB serializes updates, but debounced turns run in timer tasks outside
    # that queue, so `_chat_lock` guards every session read/write and command.
    app = Application.builder().token(token).build()
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _telegram_handle_message)
    )
    for cmd, _desc in _TELEGRAM_BOT_COMMANDS:
        app.add_handler(CommandHandler(cmd, _make_telegram_cmd_handler(cmd)))

    channels.register(_TelegramChannel(app))

    await app.initialize()
    try:
        await app.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in _TELEGRAM_BOT_COMMANDS]
        )
    except Exception as e:  # noqa: BLE001
        log.warning("failed to register Telegram bot commands: %s", e)
    await app.start()
    assert app.updater is not None
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("telegram: polling started (%d known chats)", len(_known_chats))
    return app


async def stop_telegram(app: Application) -> None:
    log.info("telegram: stopping")
    assert app.updater is not None
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
