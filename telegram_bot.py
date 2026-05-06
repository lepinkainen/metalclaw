import asyncio
import json
import logging
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

import channels
import heartbeat
import memory
import tools  # noqa: F401 — trigger @tool registrations
from config import get_config
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
    _parse_search_args,
    _format_search_result,
    _ONBOARDING_STEPS,
    _format_interests,
)
from registry import TOOLS

logging.basicConfig(level=logging.INFO)

_sessions: dict[int, list[dict]] = {}
_onboarding: dict[int, int] = {}


def _chats_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    p = Path(xdg) / "metalclaw" / "telegram_chats.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_known_chats() -> set[int]:
    path = _chats_path()
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {int(x) for x in data} if isinstance(data, list) else set()


def _save_known_chats(chats: set[int]) -> None:
    _chats_path().write_text(json.dumps(sorted(chats)), encoding="utf-8")


_known_chats: set[int] = set()


def _remember_chat(chat_id: int) -> None:
    if chat_id in _known_chats:
        return
    _known_chats.add(chat_id)
    _save_known_chats(_known_chats)


class _TelegramChannel:
    name = "telegram"

    def __init__(self, app: Application) -> None:
        self._app = app

    async def notify(self, scope: str, text: str) -> None:
        if not scope.startswith("telegram-"):
            return
        try:
            chat_id = int(scope[len("telegram-") :])
        except ValueError:
            return
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        except Exception:
            await self._app.bot.send_message(chat_id=chat_id, text=text)

    def active_scopes(self) -> Iterable[str]:
        return tuple(f"telegram-{cid}" for cid in _known_chats)


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
    _remember_chat(chat_id)
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
    "search":  ("search_vault",     _parse_search_args,  _format_search_result),
}

_HELP_TEXT = "\n".join([
    "Available commands:",
    "/train <station> [--line R] [--count 5]",
    "/weather <location>",
    "/mail [--mailbox inbox] [--unread] [--from name] [--count 10]",
    "/search <query> [--max 20] [--context 1] — search the Obsidian vault",
    "/remember <key>=<value> — save a preference",
    "/forget <substring> — remove a memory entry",
    "/memory — show stored memory",
    "/onboard — answer a few questions to seed memory",
    "/heartbeat — show heartbeat config; /heartbeat run to fire now",
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


async def _handle_heartbeat_cmd(update: Update, sub: str, chat_id: int) -> None:
    scope = _scope_for(chat_id)
    path = heartbeat.heartbeat_path_for(scope)
    if sub == "run":
        asyncio.create_task(heartbeat.run_tick())
        await update.message.reply_text("heartbeat tick fired")
        return
    from config import get_config
    cfg = get_config()
    lines = [
        f"heartbeat enabled={cfg.heartbeat_enabled} interval={cfg.heartbeat_interval_seconds}s",
        f"checklist: {path}",
    ]
    if path.exists():
        try:
            hb = heartbeat.parse_heartbeat_file(path.read_text(encoding="utf-8"))
        except ValueError as e:
            lines.append(f"parse error: {e}")
        else:
            if hb.tasks:
                for t in hb.tasks:
                    lines.append(f"  • {t.name}  every {t.interval_seconds}s")
            else:
                lines.append("(free-form body only, no tasks)")
    else:
        lines.append("no checklist — copy heartbeat.example.md (in repo root) to the path above")
    await update.message.reply_text("\n".join(lines))


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
    elif cmd == "heartbeat":
        await _handle_heartbeat_cmd(update, args.strip(), chat_id)
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


async def _async_main() -> None:
    global _known_chats
    _known_chats = _load_known_chats()

    token = get_config().telegram_bot_token
    if not token:
        raise RuntimeError(
            "telegram_bot_token missing — set TELEGRAM_BOT_TOKEN env or telegram_bot_token in config.yaml"
        )
    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    for cmd in ("help", "train", "weather", "mail", "search", "new", "remember", "forget", "memory", "onboard", "heartbeat"):
        app.add_handler(CommandHandler(cmd, _make_cmd_handler(cmd)))

    channels.register(_TelegramChannel(app))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)

    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat.run(stop))

    try:
        await asyncio.Event().wait()  # block forever; cancelled on signal
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            hb_task.cancel()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main() -> None:
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
