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
)
from config import xdg_data_dir
from frontends import common
from registry import TOOLS

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
        return set()
    return {int(x) for x in data} if isinstance(data, list) else set()


def _save_known_chats(chats: set[int]) -> None:
    _telegram_chats_path().write_text(json.dumps(sorted(chats)), encoding="utf-8")


def _remember_chat(chat_id: int) -> None:
    if chat_id in _known_chats:
        return
    _known_chats.add(chat_id)
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
        await reply(text)


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
                pass
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
            return
        html_text = telegram_format.to_html(text)
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=html_text, parse_mode="HTML")
        except Exception:
            await self._app.bot.send_message(chat_id=chat_id, text=text)

    def active_scopes(self) -> Iterable[str]:
        return tuple(common.telegram_scope(cid) for cid in _known_chats)


def _get_telegram_session(chat_id: int) -> list[dict]:
    if chat_id not in _telegram_sessions:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _telegram_sessions[chat_id] = [
            {"role": "system", "content": build_system_prompt(now)}
        ]
    return _telegram_sessions[chat_id]


_TELEGRAM_BOT_COMMANDS: list[tuple[str, str]] = common.telegram_bot_commands()

_TELEGRAM_HELP_TEXT = "\n".join(common.HELP_LINES)


async def _telegram_dispatch_command(
    update: Update, cmd: str, args: str, bot=None
) -> None:
    chat_id = update.effective_chat.id
    scope = _telegram_scope_for(chat_id)
    send = _send_for(update)
    canon = common.canonicalize(cmd) or cmd

    if canon == "help":
        await _tg_reply(update, _TELEGRAM_HELP_TEXT)
    elif canon == "new":
        old = _telegram_sessions.pop(chat_id, None)
        if old is not None:
            forget_session_provider(old)
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
            send, typing_ctx, _get_telegram_session(chat_id), args.strip()
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
            await _tg_reply(update, str(e))
            return
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
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
            await _tg_reply(update, f"Error: {e}")
            return
        await _tg_reply(update, formatter(result))
    else:
        await _tg_reply(update, f"Unknown command: /{cmd}  (try /help)")


async def _telegram_handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    _remember_chat(chat_id)
    messages = _get_telegram_session(chat_id)

    parsed = _parse_command(text)
    if parsed is not None:
        cmd, args = parsed
        await _telegram_dispatch_command(update, cmd, args, context.bot)
        return

    try:
        async with _typing(chat_id, context.bot):
            _, _, clean_reply = await run_turn(
                messages, text, lambda: chat(messages)
            )
    except Exception as e:
        await _tg_reply(update, f"Error: {e}")
        return
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
        logging.warning("failed to register Telegram bot commands: %s", e)
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    return app


async def stop_telegram(app: Application) -> None:
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
