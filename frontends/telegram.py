"""Telegram frontend: per-chat sessions, slash commands, message routing."""

import asyncio
import json
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime
from pathlib import Path

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import channels
import telegram_format
from chat_loop import (
    _parse_command,
    build_system_prompt,
    chat,
    forget_session_provider,
    run_turn,
    scoped_chat,
)
from config import get_config, xdg_data_dir
from frontends import common
from registry import TOOLS

log = logging.getLogger(__name__)

_telegram_sessions: dict[int, list[dict]] = {}
_known_chats: set[int] = set()


def _telegram_chats_path() -> Path:
    return xdg_data_dir() / "telegram_chats.json"


def _load_known_chats() -> set[int]:
    path = _telegram_chats_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("telegram: could not read known chats from %s", path)
        return set()
    return {int(x) for x in data} if isinstance(data, list) else set()


def _save_known_chats(chats: set[int]) -> None:
    _telegram_chats_path().write_text(json.dumps(sorted(chats)), encoding="utf-8")


def _remember_chat(chat_id: int) -> None:
    if chat_id in _known_chats:
        return
    _known_chats.add(chat_id)
    log.info("telegram: new chat remembered chat_id=%d (known=%d)", chat_id, len(_known_chats))
    _save_known_chats(_known_chats)


def _telegram_scope_for(chat_id: int) -> str:
    return common.telegram_scope(chat_id)


def known_chat_count() -> int:
    return len(_known_chats)


async def _tg_reply(update: Update, text: str) -> None:
    """Send a Telegram reply, rendering CommonMark as Telegram HTML."""
    html_text = telegram_format.to_html(text)
    reply = update.message.reply_text
    try:
        await reply(html_text, parse_mode="HTML")
    except Exception:
        log.warning("telegram: HTML reply failed, falling back to plain text", exc_info=True)
        await reply(text)
    log.debug("telegram: replied %d chars", len(text))


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
        except (asyncio.CancelledError, Exception):
            pass


class _TelegramChannel:
    name = "telegram"

    def __init__(self, app: Application) -> None:
        self._app = app

    async def notify(self, scope: str, text: str) -> None:
        chat_id = common.parse_telegram_scope(scope)
        if chat_id is None:
            log.warning("telegram: notify with unparseable scope %r", scope)
            return
        log.info("telegram: notify chat_id=%d %d chars", chat_id, len(text))
        html_text = telegram_format.to_html(text)
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=html_text, parse_mode="HTML")
        except Exception:
            log.warning("telegram: HTML notify failed, retrying plain", exc_info=True)
            await self._app.bot.send_message(chat_id=chat_id, text=text)

    def active_scopes(self) -> Iterable[str]:
        return tuple(common.telegram_scope(cid) for cid in _known_chats)


def _get_telegram_session(chat_id: int) -> list[dict]:
    if chat_id not in _telegram_sessions:
        log.info("telegram: new session chat_id=%d", chat_id)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _telegram_sessions[chat_id] = [
            {"role": "system", "content": build_system_prompt(now)}
        ]
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
    chat_id = update.effective_chat.id
    scope = _telegram_scope_for(chat_id)
    send = _send_for(update)
    canon = common.canonicalize(cmd) or cmd
    log.info("telegram: command /%s (canon=%s) chat_id=%d args=%d chars", cmd, canon, chat_id, len(args))

    if canon == "help":
        await _tg_reply(update, _TELEGRAM_HELP_TEXT)
    elif canon == "tools":
        cur = _telegram_show_tools.get(chat_id, get_config().show_tool_calls)
        new, reply = common.parse_tools_arg(cur, args)
        _telegram_show_tools[chat_id] = new
        await _tg_reply(update, reply)
    elif canon == "new":
        old = _telegram_sessions.pop(chat_id, None)
        if old is not None:
            forget_session_provider(old)
        log.info("telegram: session reset chat_id=%d (had_session=%s)", chat_id, old is not None)
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
        log.info("telegram: tool command /%s -> %s params=%.200s", cmd, tool_name, params)
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
            log.warning("telegram: tool %s unavailable", tool_name)
            await _tg_reply(update, f"{tool_name} tool unavailable")
            return
        loop = asyncio.get_running_loop()
        try:
            if bot is not None:
                async with _typing(chat_id, bot):
                    result = await loop.run_in_executor(None, lambda: tool_obj.func(**params))
            else:
                result = await loop.run_in_executor(None, lambda: tool_obj.func(**params))
        except Exception as e:
            log.exception("telegram: tool command /%s failed", cmd)
            await _tg_reply(update, f"Error: {e}")
            return
        await _tg_reply(update, formatter(result))
    else:
        log.info("telegram: unknown command /%s", cmd)
        await _tg_reply(update, f"Unknown command: /{cmd}  (try /help)")


async def _telegram_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    user = update.effective_user
    log.info(
        "telegram: message chat_id=%d user=%s len=%d",
        chat_id, getattr(user, "username", None) or getattr(user, "id", None), len(text),
    )
    log.debug("telegram: message text: %.500s", text)
    _remember_chat(chat_id)
    messages = _get_telegram_session(chat_id)

    parsed = _parse_command(text)
    if parsed is not None:
        cmd, args = parsed
        await _telegram_dispatch_command(update, cmd, args, context.bot)
        return

    on_tool = _tool_status_for(chat_id, context.bot)
    try:
        async with _typing(chat_id, context.bot):
            _, _, clean_reply = await run_turn(
                messages,
                text,
                lambda: scoped_chat(
                    _telegram_scope_for(chat_id),
                    lambda: chat(messages, on_tool_call=on_tool),
                ),
            )
    except Exception as e:
        log.exception("telegram: turn failed chat_id=%d", chat_id)
        await _tg_reply(update, f"Error: {e}")
        return
    if on_tool is not None:
        await on_tool.drain()
    log.info("telegram: reply chat_id=%d len=%d", chat_id, len(clean_reply))
    await _tg_reply(update, clean_reply)


def _make_telegram_cmd_handler(cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _telegram_dispatch_command(
            update, cmd, " ".join(context.args or []), context.bot
        )
    return handler


async def start_telegram(token: str) -> Application:
    global _known_chats
    _known_chats = _load_known_chats()

    # PTB's Application serializes updates per-chat by default, so the
    # `_telegram_sessions` list mutation in handlers needs no per-chat lock
    # (cf. the explicit asyncio.Lock in frontends/discord.py). If anyone ever
    # passes `concurrent_updates=True` to the builder below, add a per-chat
    # lock around session reads/writes the same way Discord does.
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, _telegram_handle_message))
    for cmd, _desc in _TELEGRAM_BOT_COMMANDS:
        app.add_handler(CommandHandler(cmd, _make_telegram_cmd_handler(cmd)))

    channels.register(_TelegramChannel(app))

    await app.initialize()
    try:
        await app.bot.set_my_commands(
            [BotCommand(cmd, desc) for cmd, desc in _TELEGRAM_BOT_COMMANDS]
        )
    except Exception as e:
        log.warning("failed to register Telegram bot commands: %s", e)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    log.info("telegram: polling started (%d known chats)", len(_known_chats))
    return app


async def stop_telegram(app: Application) -> None:
    log.info("telegram: stopping")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
