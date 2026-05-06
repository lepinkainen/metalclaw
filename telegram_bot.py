import asyncio
import logging
import os
from datetime import datetime

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import memory
import tools  # noqa: F401 — trigger @tool registrations
from bot import (
    build_system_prompt,
    chat,
    _split_thinking,
    _parse_command,
    _parse_train_args,
    _format_train_result,
    _parse_weather_args,
    _format_weather_result,
    _parse_mail_args,
    _format_mail_result,
    _ONBOARDING_STEPS,
    _format_interests,
)
from registry import TOOLS

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

_sessions: dict[int, list[dict]] = {}
_onboarding: dict[int, int] = {}


def _scope_for(chat_id: int) -> str:
    return f"telegram-{chat_id}"


def _get_session(chat_id: int) -> list[dict]:
    if chat_id not in _sessions:
        scope = _scope_for(chat_id)
        memory.current_scope.set(scope)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _sessions[chat_id] = [
            {"role": "system", "content": build_system_prompt(scope, now)}
        ]
    return _sessions[chat_id]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    memory.current_scope.set(_scope_for(chat_id))
    messages = _get_session(chat_id)

    parsed = _parse_command(text)
    if parsed is not None:
        cmd, args = parsed
        await _dispatch_command(update, cmd, args, messages)
        return

    if chat_id in _onboarding:
        await _handle_onboarding_answer(update, chat_id, text)
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
    "/remember <key>=<value> — save a preference",
    "/forget <substring> — remove a memory entry",
    "/memory — show stored memory",
    "/onboard — answer a few questions to seed memory",
    "/new — reset this conversation",
    "/help — this message",
])


async def _start_onboarding(update: Update, chat_id: int) -> None:
    memory.current_scope.set(_scope_for(chat_id))
    if memory.load().preferences:
        await update.message.reply_text(
            "Already onboarded. Use /memory to inspect or /forget to remove entries."
        )
        return
    _onboarding[chat_id] = 0
    _, question = _ONBOARDING_STEPS[0]
    await update.message.reply_text(
        f"Onboarding — answer briefly, send '-' to skip.\n\n{question}"
    )


async def _handle_onboarding_answer(update: Update, chat_id: int, text: str) -> None:
    memory.current_scope.set(_scope_for(chat_id))
    step = _onboarding[chat_id]
    key, _ = _ONBOARDING_STEPS[step]
    if text != "-" and text.strip():
        value = _format_interests(text) if key == "interests" else text.strip()
        memory.set_preference(key, value)

    next_step = step + 1
    if next_step >= len(_ONBOARDING_STEPS):
        del _onboarding[chat_id]
        _sessions.pop(chat_id, None)
        await update.message.reply_text(
            "Onboarding done. Memory will enter the system prompt on next message."
        )
        return

    _onboarding[chat_id] = next_step
    _, question = _ONBOARDING_STEPS[next_step]
    await update.message.reply_text(question)


async def _dispatch_command(
    update: Update, cmd: str, args: str, messages: list[dict]
) -> None:
    chat_id = update.effective_chat.id
    if cmd == "help":
        await update.message.reply_text(_HELP_TEXT)
    elif cmd == "new":
        _sessions.pop(chat_id, None)
        _onboarding.pop(chat_id, None)
        await update.message.reply_text("Conversation reset.")
    elif cmd == "remember":
        if "=" not in args:
            await update.message.reply_text("usage: /remember <key>=<value>")
            return
        key, value = args.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            await update.message.reply_text("usage: /remember <key>=<value>")
            return
        memory.set_preference(key, value)
        await update.message.reply_text(f"saved {key}={value}")
    elif cmd == "forget":
        matcher = args.strip()
        if not matcher:
            await update.message.reply_text("usage: /forget <substring>")
            return
        if memory.forget(matcher):
            await update.message.reply_text(f"forgot entry matching '{matcher}'")
        else:
            await update.message.reply_text(f"no entry matched '{matcher}'")
    elif cmd == "memory":
        await update.message.reply_text(memory.render_full())
    elif cmd == "onboard":
        await _start_onboarding(update, chat_id)
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
        chat_id = update.effective_chat.id
        memory.current_scope.set(_scope_for(chat_id))
        await _dispatch_command(
            update, cmd, " ".join(context.args or []), _get_session(chat_id)
        )
    return handler


def main() -> None:
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    for cmd in ("help", "train", "weather", "mail", "new", "remember", "forget", "memory", "onboard"):
        app.add_handler(CommandHandler(cmd, _make_cmd_handler(cmd)))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
