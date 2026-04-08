import asyncio
import logging
import os
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import tools  # noqa: F401 — trigger @tool registrations
from bot import (
    SYSTEM_PROMPT,
    chat,
    _split_thinking,
    _parse_command,
    _parse_train_args,
    _format_train_result,
    _parse_weather_args,
    _format_weather_result,
    _parse_mail_args,
    _format_mail_result,
)
from registry import TOOLS

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_sessions: dict[int, list[dict]] = {}


def _get_session(chat_id: int) -> list[dict]:
    if chat_id not in _sessions:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _sessions[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT.format(now=now)}]
    return _sessions[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    messages = _get_session(chat_id)

    parsed = _parse_command(text)
    if parsed is not None:
        cmd, args = parsed
        await _dispatch_command(update, cmd, args, messages)
        return

    messages.append({"role": "user", "content": text})
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, lambda: chat(messages))
    except Exception as e:
        messages.pop()
        await update.message.reply_text(f"Error: {e}")
        return
    messages.append({"role": "assistant", "content": reply})
    _, clean_reply = _split_thinking(reply)
    await update.message.reply_text(clean_reply)


_TOOL_COMMANDS: dict[str, tuple] = {
    "train":   ("train_departures", _parse_train_args,   _format_train_result),
    "weather": ("weather",          _parse_weather_args, _format_weather_result),
    "mail":    ("list_emails",      _parse_mail_args,    _format_mail_result),
}

_HELP_TEXT = "\n".join([
    "Available commands:",
    "/train <station> [--line R] [--count 5]",
    "/weather <location>",
    "/mail [--mailbox inbox] [--unread] [--from name] [--count 10]",
    "/new \u2014 reset this conversation",
    "/help \u2014 this message",
])


async def _dispatch_command(
    update: Update, cmd: str, args: str, messages: list[dict]
) -> None:
    if cmd == "help":
        await update.message.reply_text(_HELP_TEXT)
    elif cmd == "new":
        _sessions.pop(update.effective_chat.id, None)
        await update.message.reply_text("Conversation reset.")
    elif cmd in _TOOL_COMMANDS:
        tool_name, parser, formatter = _TOOL_COMMANDS[cmd]
        try:
            params = parser(args)
        except ValueError as e:
            await update.message.reply_text(str(e))
            return
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
            await update.message.reply_text(f"{tool_name} tool unavailable")
            return
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: tool_obj.func(**params))
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")
            return
        await update.message.reply_text(formatter(result))
    else:
        await update.message.reply_text(f"Unknown command: /{cmd}  (try /help)")


def _make_cmd_handler(cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await _dispatch_command(
            update, cmd, " ".join(context.args or []), _get_session(update.effective_chat.id)
        )
    return handler


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    for cmd in ("help", "train", "weather", "mail", "new"):
        app.add_handler(CommandHandler(cmd, _make_cmd_handler(cmd)))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
