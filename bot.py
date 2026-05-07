import argparse
import asyncio
import json
import logging
import os
import re
import shlex
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import NoReturn

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown
from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import channels
import heartbeat
import memory
import self_change
import telegram_format
from config import get_config
from history import SQLiteHistory
from providers import Provider, get_provider
from registry import TOOLS

REPO_ROOT = Path(__file__).parent.resolve()

console = Console(highlight=False)

_COMMANDS = {
    "add-tool": "add a new tool — describe what it should do",
    "self-edit": "make a general code change — describe what to change",
    "think": "toggle display of model thinking (off by default)",
    "train": "show train departures: /train <station> [--line R] [--count 5]",
    "weather": "show weather: /weather <location>",
    "mail": "show emails: /mail [--mailbox inbox] [--unread] [--from name] [--count 10]",
    "search": "search the Obsidian vault: /search <query> [--max 20] [--context 1]",
    "remember": "save a preference: /remember <key>=<value>",
    "forget": "remove a memory entry: /forget <matcher>",
    "memory": "show your stored long-term memory",
    "onboard": "answer a few questions to seed long-term memory",
    "heartbeat": "show heartbeat config / run a tick now (/heartbeat run)",
    "big": "ask the escalation cloud model directly: /big <query>",
    "help": "show this help",
}

_show_thinking = False
_prompt_session: PromptSession | None = None
_ONBOARDING_STEPS: list[tuple[str, str]] = [
    ("role", "What's your role / what do you work on?"),
    ("interests", "What topics matter to you? (comma-separated)"),
    ("tone", "Preferred tone? (e.g. terse, friendly, formal)"),
    ("location", "Timezone or city?"),
]

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _split_thinking(text: str) -> tuple[str, str]:
    """Return (thinking_content, reply_without_think_tags)."""
    thinking_parts = _THINK_RE.findall(text)
    clean = _THINK_RE.sub("", text).strip()
    return "\n".join(thinking_parts).strip(), clean


def _parse_command(text: str) -> tuple[str, str] | None:
    """Return (command, args) if text starts with /, else None."""
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


_SYSTEM_PROMPT_BASE = (
    "You are Metalclaw, a helpful assistant. "
    "The current date and time is {now}. "
    "When a user request can be fulfilled by calling a tool, you MUST use the tool "
    "rather than simulating or writing code. Never generate fake results. "
    "When a tool returns factual data, present it directly and naturally. "
    "Do not add generic warnings, hedging, or suggestions to verify elsewhere unless "
    "the tool result itself indicates uncertainty, staleness, or an error. "
    "If a tool returns source metadata, treat it as authoritative context for how to "
    "describe the result. "
    "You have long-term memory across sessions: call set_user_preference, "
    "add_user_fact, or forget_user_memory to record durable information about the "
    "user; call get_user_memory to read the full file. "
    "If a request needs more info, check memory first before asking the user. "
    "When the user gives information, store it in memory if relevant."
)


_ESCALATION_HINT = (
    "When a question is beyond your capability — complex reasoning, deep code "
    "analysis, niche knowledge — call escalate_to_big_model rather than "
    "guessing. Pass the user's question and a short reason. Do not escalate "
    "trivial requests."
)


def build_system_prompt(scope: str, now: str) -> str:
    base = _SYSTEM_PROMPT_BASE.format(now=now)
    cfg = get_config()
    if cfg.escalation_enabled and cfg.provider == "ollama":
        base += "\n\n" + _ESCALATION_HINT
    summary = memory.summary(scope)
    if summary:
        base += f"\n\nKnown about user (scope={scope}):\n{summary}"
    return base


def _refresh_system_prompt(messages: list[dict], scope: str) -> None:
    """Rewrite messages[0] with current memory summary so mid-session writes are visible."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = build_system_prompt(scope, now)
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": prompt}
    else:
        messages.insert(0, {"role": "system", "content": prompt})

# --- Provider-agnostic chat loop ---


def _tool_result_json(result: object) -> str:
    return json.dumps(result, ensure_ascii=False)


# Snapshot of the current session's messages list, set on each chat() entry so
# tools (notably escalate_to_big_model) can read full conversation context.
_active_session_messages: ContextVar[list[dict] | None] = ContextVar(
    "active_session_messages", default=None
)


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    if messages and messages[0].get("role") == "system":
        return messages[0].get("content", "") or "", list(messages[1:])
    return "", list(messages)


def _run_tool(name: str, args: dict) -> object:
    tool_obj = TOOLS.get(name)
    if tool_obj is None:
        return f"Error: unknown tool '{name}'"
    try:
        return tool_obj.func(**args)
    except Exception as e:
        return f"Error: {e}"


def _chat_with_provider(
    provider: Provider,
    messages: list[dict],
    *,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    exclude_tools: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Run the tool-call loop against `provider`, mutating `messages` in place."""
    tool_schemas = [
        t.schema for name, t in TOOLS.items() if name not in exclude_tools
    ]
    system, history = _split_system(messages)
    token = _active_session_messages.set(messages)
    try:
        while True:
            am = provider.chat_once(history, tool_schemas, system)
            raw = am.raw
            if isinstance(raw, list):
                history.extend(raw)
            elif raw is not None:
                history.append(raw)

            if not am.tool_calls:
                messages[:] = (
                    [{"role": "system", "content": system}] if system else []
                ) + history
                return am.text

            results: list[tuple] = []
            for tc in am.tool_calls:
                result = _run_tool(tc.name, tc.arguments)
                result_json = _tool_result_json(result)
                if on_tool_call:
                    on_tool_call(tc.name, tc.arguments, result_json[:120])
                results.append((tc, result_json))
            history.extend(provider.format_tool_results(results))
    finally:
        _active_session_messages.reset(token)


def chat(
    messages: list[dict],
    on_tool_call: Callable[[str, dict, str], None] | None = None,
) -> str:
    """Send messages via the configured provider, handle tool calls, return final text.

    Mutates `messages` in place, appending tool-call/result entries so the
    caller retains full conversation history.
    """
    cfg = get_config()
    provider = get_provider(cfg.provider)
    return _chat_with_provider(provider, messages, on_tool_call=on_tool_call)


def chat_via_escalation(messages: list[dict]) -> str:
    """Run a chat turn through the escalation provider, no recursion into itself."""
    cfg = get_config()
    if not cfg.escalation_enabled:
        raise RuntimeError("escalation is disabled in config")
    big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    return _chat_with_provider(
        big, messages, exclude_tools={"escalate_to_big_model"}
    )


# --- CLI ---


def _format_weather_result(result: dict) -> str:
    location = result["location"]["display_name"]
    current = result["current"]
    lines = [f"Weather for {location}:"]
    lines.append(
        f"Now: {current['condition']}, {current['temperature_c']}°C, wind {current['wind_m_s']} m/s"
    )

    if result.get("today"):
        today = result["today"]
        lines.append(
            f"Today: {today['condition']}, {today['temperature_low_c']}–{today['temperature_high_c']}°C"
        )
    if result.get("tomorrow"):
        tomorrow = result["tomorrow"]
        lines.append(
            f"Tomorrow: {tomorrow['condition']}, {tomorrow['temperature_low_c']}–{tomorrow['temperature_high_c']}°C"
        )

    return "\n".join(lines)


def _format_train_result(result: dict) -> str:
    station = result["station"]
    line = result.get("line_filter")
    header = f"Departures from {station['name']} ({station['code']})"
    if line:
        header += f" for line {line}"

    departures = result.get("departures", [])
    if not departures:
        if line:
            return f"No upcoming {line} departures found for {station['name']} ({station['code']})."
        return f"No upcoming departures found for {station['name']} ({station['code']})."

    lines = [f"{header}:"]
    for dep in departures:
        name = dep["line"] or f"{dep['train_type']} {dep['train_number']}".strip()
        if dep["line"] and not line:
            name += f" ({dep['train_type']} {dep['train_number']})"

        scheduled = dep["scheduled_time"][11:16]
        time_str = scheduled
        if dep.get("actual_time") and dep["actual_time"] != dep["scheduled_time"]:
            time_str += f" (actual {dep['actual_time'][11:16]})"
        elif dep.get("estimated_time") and dep["estimated_time"] != dep["scheduled_time"]:
            time_str += f" (est. {dep['estimated_time'][11:16]})"

        cancelled = " [CANCELLED]" if dep.get("cancelled") else ""
        lines.append(
            f"  {time_str}  {name:<14}  track {dep['track']}  -> {dep['destination_code']}{cancelled}"
        )

    return "\n".join(lines)


def _print_help() -> None:
    for name, desc in _COMMANDS.items():
        console.print(f"  /{name}  —  {desc}")


def _handle_add_tool(args: str) -> None:
    request = f"Add a new tool: {args}"
    result = self_change.run_self_change(request, REPO_ROOT)
    status = "approved" if result.approved else "rejected"
    console.print(f"\n[dim][self-change {status}][/dim]\n")


def _handle_self_edit(args: str) -> None:
    result = self_change.run_self_change(args, REPO_ROOT)
    status = "approved" if result.approved else "rejected"
    console.print(f"\n[dim][self-change {status}][/dim]\n")


def _handle_think(_: str) -> None:
    global _show_thinking
    _show_thinking = not _show_thinking
    state = "on" if _show_thinking else "off"
    console.print(f"[dim]thinking display {state}[/dim]")


class _ArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def _parse_train_args(args: str) -> dict:
    p = _ArgParser(prog="/train", add_help=False)
    p.add_argument("station")
    p.add_argument("--line", default=None)
    p.add_argument("--count", type=int, default=5)
    ns = p.parse_args(shlex.split(args))
    return {"station": ns.station, "line": ns.line, "count": ns.count}


def _parse_weather_args(args: str) -> dict:
    try:
        parts = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"invalid /weather arguments: {e}") from e
    if not parts:
        raise ValueError("usage: /weather <location>")
    return {"location": " ".join(parts)}


def _format_mail_result(result: dict) -> str:
    name = result["mailbox"]
    total = result.get("total_emails")
    unread = result.get("unread_emails")
    if total is None and unread is None:
        header = f"{name}: {len(result.get('emails', []))} returned"
    else:
        header = f"{name}: {total} total, {unread} unread"
    emails = result.get("emails", [])
    if not emails:
        return f"{header}\n\nNo emails found."
    lines = [header, ""]
    for i, e in enumerate(emails, 1):
        unread_tag = " [UNREAD]" if e.get("unread") else ""
        received = e.get("received_at", "")[:10]
        folders = e.get("folders") or []
        folder_tag = f"  ({', '.join(folders)})" if folders else ""
        lines.append(f"{i}. {e['from']}  |  {received}{unread_tag}{folder_tag}")
        lines.append(f"   {e['subject']}")
        if e.get("preview"):
            lines.append(f"   {e['preview'][:100]}")
    return "\n".join(lines)


def _parse_mail_args(args: str) -> dict:
    p = _ArgParser(prog="/mail", add_help=False)
    p.add_argument("--mailbox", default="inbox")
    p.add_argument("--unread", action="store_true")
    p.add_argument("--from", dest="from_search", default=None)
    p.add_argument("--count", type=int, default=10)
    ns = p.parse_args(shlex.split(args))
    return {"mailbox": ns.mailbox, "unread_only": ns.unread, "from_search": ns.from_search, "limit": ns.count}


def _parse_search_args(args: str) -> dict:
    p = _ArgParser(prog="/search", add_help=False)
    p.add_argument("--max", dest="max_results", type=int, default=20)
    p.add_argument("--context", dest="context_lines", type=int, default=1)
    p.add_argument("query", nargs="+")
    try:
        parts = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"invalid /search arguments: {e}") from e
    if not parts:
        raise ValueError("usage: /search <query> [--max N] [--context N]")
    ns = p.parse_args(parts)
    return {
        "query": " ".join(ns.query),
        "max_results": ns.max_results,
        "context_lines": ns.context_lines,
    }


def _format_search_result(result: dict) -> str:
    hits = result.get("hits", [])
    query = result.get("query", "")
    if not hits:
        return f"No matches for `{query}` in the vault."
    lines = [f"**{len(hits)}** match(es) for `{query}`"]
    if result.get("truncated"):
        lines[0] += " (truncated)"
    lines.append("")
    for hit in hits:
        path = hit.get("path", "?")
        line_no = hit.get("line_number", "?")
        snippet = hit.get("line", "")
        lines.append(f"- **{path}:{line_no}** — {snippet}")
        for ctx in hit.get("before", []):
            lines.append(f"    {ctx}")
        for ctx in hit.get("after", []):
            lines.append(f"    {ctx}")
    return "\n".join(lines)


def _print_bot_markdown(text: str) -> None:
    console.print()
    console.print("[bold]bot>[/bold]")
    console.print(Markdown(text))
    console.print()



def _make_tool_handler(tool_name: str, parser: Callable, formatter: Callable) -> Callable:
    def handler(args: str) -> None:
        params = parser(args)
        tool_obj = TOOLS.get(tool_name)
        if tool_obj is None:
            console.print(f"{tool_name} tool is not available")
            return
        try:
            result = tool_obj.func(**params)
        except Exception as e:
            console.print(f"\n[bold]bot>[/bold] Error: {e}\n")
            return
        _print_bot_markdown(formatter(result))
    return handler


def _handle_help(_: str) -> None:
    _print_help()


def _handle_big(args: str) -> None:
    query = args.strip()
    if not query:
        console.print("usage: /big <query>")
        return
    cfg = get_config()
    if not cfg.escalation_enabled:
        console.print("escalation disabled — set escalation_enabled: true in config.yaml")
        return
    messages = _cli_messages_ref()
    if messages is None:
        console.print("internal error: no active CLI session")
        return
    _refresh_system_prompt(messages, "cli")
    messages.append({"role": "user", "content": query})
    try:
        with console.status("[dim]asking the big model…[/dim]", spinner="dots"):
            reply = chat_via_escalation(messages)
    except Exception as e:
        messages.pop()
        console.print(f"\n[bold]bot>[/bold] Error: {e}\n")
        return
    messages.append({"role": "assistant", "content": reply})
    _, clean_reply = _split_thinking(reply)
    _print_bot_markdown(clean_reply)


_cli_messages: list[dict] | None = None


def _cli_messages_ref() -> list[dict] | None:
    return _cli_messages


def _handle_remember(args: str) -> None:
    if "=" not in args:
        console.print("usage: /remember <key>=<value>")
        return
    key, value = args.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key or not value:
        console.print("usage: /remember <key>=<value>")
        return
    memory.set_preference(key, value)
    console.print(f"[dim]saved {key}={value}[/dim]")


def _handle_forget(args: str) -> None:
    matcher = args.strip()
    if not matcher:
        console.print("usage: /forget <substring>")
        return
    if memory.forget(matcher):
        console.print(f"[dim]forgot entry matching '{matcher}'[/dim]")
    else:
        console.print(f"no entry matched '{matcher}'")


def _handle_memory(_: str) -> None:
    _print_bot_markdown(memory.render_full())


def _format_interests(raw: str) -> str:
    items = [s.strip() for s in raw.split(",") if s.strip()]
    return ", ".join(f"[[{i}]]" for i in items) if items else raw


def _handle_heartbeat(args: str) -> None:
    sub = args.strip()
    path = heartbeat.heartbeat_path_for(memory.current_scope.get())
    if sub == "run":
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(heartbeat.run_tick())
        else:
            asyncio.create_task(heartbeat.run_tick())
        console.print("[dim]heartbeat tick fired[/dim]")
        return
    cfg = get_config()
    console.print(f"[dim]heartbeat enabled={cfg.heartbeat_enabled} interval={cfg.heartbeat_interval_seconds}s[/dim]")
    console.print(f"[dim]checklist: {path}[/dim]")
    if path.exists():
        try:
            hb = heartbeat.parse_heartbeat_file(path.read_text(encoding="utf-8"))
        except ValueError as e:
            console.print(f"[red]parse error: {e}[/red]")
            return
        if hb.tasks:
            for t in hb.tasks:
                console.print(f"  • {t.name}  every {t.interval_seconds}s")
        else:
            console.print("[dim]no tasks defined (free-form body only)[/dim]")
    else:
        template = REPO_ROOT / "heartbeat.example.md"
        console.print(
            f"[dim]no checklist — copy {template} to the path above to opt in[/dim]"
        )


_pending_onboarding: list[tuple[str, str]] = []


def _handle_onboard(_: str) -> None:
    mem = memory.load()
    if mem.preferences:
        console.print(
            "[dim]already onboarded — use /memory to inspect, /forget to remove entries[/dim]"
        )
        return
    _pending_onboarding[:] = list(_ONBOARDING_STEPS)
    key, question = _pending_onboarding[0]
    console.print("[dim]onboarding — answer briefly at the prompt, '-' to skip a step[/dim]")
    console.print(f"[dim]{question}[/dim]")


_COMMAND_HANDLERS = {
    "add-tool":  _handle_add_tool,
    "self-edit": _handle_self_edit,
    "think":     _handle_think,
    "train":     _make_tool_handler("train_departures", _parse_train_args,   _format_train_result),
    "weather":   _make_tool_handler("weather",          _parse_weather_args, _format_weather_result),
    "mail":      _make_tool_handler("list_emails",      _parse_mail_args,    _format_mail_result),
    "search":    _make_tool_handler("search_vault",     _parse_search_args,  _format_search_result),
    "remember":  _handle_remember,
    "forget":    _handle_forget,
    "memory":    _handle_memory,
    "onboard":   _handle_onboard,
    "heartbeat": _handle_heartbeat,
    "big":       _handle_big,
    "help":      _handle_help,
}


def _cli_tool_log(name: str, args: dict, short_result: str) -> None:
    if not _show_thinking:
        return
    args_summary = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
    console.print(f"  [dim][tool: {name}({args_summary})] → {short_result}[/dim]")


class _CLIChannel:
    name = "cli"

    async def notify(self, scope: str, text: str) -> None:
        def _print() -> None:
            console.print()
            console.print("[bold cyan]heartbeat>[/bold cyan]")
            console.print(Markdown(text))
            console.print()

        run_in_terminal(_print)

    def active_scopes(self) -> Iterable[str]:
        return ("cli",)


# --- Telegram ---


_telegram_sessions: dict[int, list[dict]] = {}
_telegram_onboarding: dict[int, int] = {}
_known_chats: set[int] = set()


def _telegram_chats_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    p = Path(xdg) / "metalclaw" / "telegram_chats.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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
    return f"telegram-{chat_id}"


async def _tg_reply(update: Update, text: str) -> None:
    """Send a Telegram reply, rendering CommonMark as Telegram HTML."""
    html_text = telegram_format.to_html(text)
    reply = update.message.reply_text
    try:
        await reply(html_text, parse_mode="HTML")
    except Exception:
        await reply(text)


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
        if not scope.startswith("telegram-"):
            return
        try:
            chat_id = int(scope[len("telegram-") :])
        except ValueError:
            return
        html_text = telegram_format.to_html(text)
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=html_text, parse_mode="HTML")
        except Exception:
            await self._app.bot.send_message(chat_id=chat_id, text=text)

    def active_scopes(self) -> Iterable[str]:
        return tuple(f"telegram-{cid}" for cid in _known_chats)


def _get_telegram_session(chat_id: int) -> list[dict]:
    if chat_id not in _telegram_sessions:
        scope = _telegram_scope_for(chat_id)
        memory.current_scope.set(scope)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        _telegram_sessions[chat_id] = [
            {"role": "system", "content": build_system_prompt(scope, now)}
        ]
    return _telegram_sessions[chat_id]


_TELEGRAM_TOOL_COMMANDS: dict[str, tuple] = {
    "train":   ("train_departures", _parse_train_args,   _format_train_result),
    "weather": ("weather",          _parse_weather_args, _format_weather_result),
    "mail":    ("list_emails",      _parse_mail_args,    _format_mail_result),
    "search":  ("search_vault",     _parse_search_args,  _format_search_result),
}

_TELEGRAM_BOT_COMMANDS: list[tuple[str, str]] = [
    ("help",      "show available commands"),
    ("train",     "train departures: <station> [--line R] [--count 5]"),
    ("weather",   "weather for a location"),
    ("mail",      "list emails [--mailbox] [--unread] [--from] [--count]"),
    ("search",    "search the Obsidian vault"),
    ("remember",  "save a preference: <key>=<value>"),
    ("forget",    "remove a memory entry"),
    ("memory",    "show stored long-term memory"),
    ("onboard",   "seed memory by answering a few questions"),
    ("heartbeat", "show heartbeat config (or 'run' to fire now)"),
    ("big",       "ask the escalation cloud model directly"),
    ("new",       "reset this conversation"),
]

_TELEGRAM_HELP_TEXT = "\n".join([
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
    "/big <query> — ask the escalation cloud model directly",
    "/new — reset this conversation",
    "/help — this message",
])


async def _telegram_start_onboarding(update: Update, chat_id: int) -> None:
    memory.current_scope.set(_telegram_scope_for(chat_id))
    if memory.load().preferences:
        await _tg_reply(update, 
            "Already onboarded. Use /memory to inspect or /forget to remove entries."
        )
        return
    _telegram_onboarding[chat_id] = 0
    _, question = _ONBOARDING_STEPS[0]
    await _tg_reply(update, 
        f"Onboarding — answer briefly, send '-' to skip.\n\n{question}"
    )


async def _telegram_handle_onboarding_answer(update: Update, chat_id: int, text: str) -> None:
    memory.current_scope.set(_telegram_scope_for(chat_id))
    step = _telegram_onboarding[chat_id]
    key, _ = _ONBOARDING_STEPS[step]
    if text != "-" and text.strip():
        value = _format_interests(text) if key == "interests" else text.strip()
        memory.set_preference(key, value)

    next_step = step + 1
    if next_step >= len(_ONBOARDING_STEPS):
        del _telegram_onboarding[chat_id]
        _telegram_sessions.pop(chat_id, None)
        await _tg_reply(update, 
            "Onboarding done. Memory will enter the system prompt on next message."
        )
        return

    _telegram_onboarding[chat_id] = next_step
    _, question = _ONBOARDING_STEPS[next_step]
    await _tg_reply(update, question)


async def _telegram_heartbeat_cmd(update: Update, sub: str, chat_id: int) -> None:
    scope = _telegram_scope_for(chat_id)
    path = heartbeat.heartbeat_path_for(scope)
    if sub == "run":
        asyncio.create_task(heartbeat.run_tick())
        await _tg_reply(update, "heartbeat tick fired")
        return
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
    await _tg_reply(update, "\n".join(lines))


async def _telegram_big(update: Update, query: str, chat_id: int, bot) -> None:
    if not query:
        await _tg_reply(update, "usage: /big <query>")
        return
    cfg = get_config()
    if not cfg.escalation_enabled:
        await _tg_reply(
            update, "escalation disabled — set escalation_enabled: true in config.yaml"
        )
        return
    scope = _telegram_scope_for(chat_id)
    messages = _get_telegram_session(chat_id)
    _refresh_system_prompt(messages, scope)
    messages.append({"role": "user", "content": query})
    loop = asyncio.get_running_loop()
    try:
        if bot is not None:
            async with _typing(chat_id, bot):
                reply = await loop.run_in_executor(None, lambda: chat_via_escalation(messages))
        else:
            reply = await loop.run_in_executor(None, lambda: chat_via_escalation(messages))
    except Exception as e:
        messages.pop()
        await _tg_reply(update, f"Error: {e}")
        return
    messages.append({"role": "assistant", "content": reply})
    _, clean_reply = _split_thinking(reply)
    await _tg_reply(update, clean_reply)


async def _telegram_dispatch_command(
    update: Update, cmd: str, args: str, bot=None
) -> None:
    chat_id = update.effective_chat.id
    if cmd == "help":
        await _tg_reply(update, _TELEGRAM_HELP_TEXT)
    elif cmd == "new":
        _telegram_sessions.pop(chat_id, None)
        _telegram_onboarding.pop(chat_id, None)
        await _tg_reply(update, "Conversation reset.")
    elif cmd == "remember":
        if "=" not in args:
            await _tg_reply(update, "usage: /remember <key>=<value>")
            return
        key, value = args.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            await _tg_reply(update, "usage: /remember <key>=<value>")
            return
        memory.set_preference(key, value)
        await _tg_reply(update, f"saved {key}={value}")
    elif cmd == "forget":
        matcher = args.strip()
        if not matcher:
            await _tg_reply(update, "usage: /forget <substring>")
            return
        if memory.forget(matcher):
            await _tg_reply(update, f"forgot entry matching '{matcher}'")
        else:
            await _tg_reply(update, f"no entry matched '{matcher}'")
    elif cmd == "memory":
        await _tg_reply(update, memory.render_full())
    elif cmd == "onboard":
        await _telegram_start_onboarding(update, chat_id)
    elif cmd == "heartbeat":
        await _telegram_heartbeat_cmd(update, args.strip(), chat_id)
    elif cmd == "big":
        await _telegram_big(update, args.strip(), chat_id, bot)
    elif cmd in _TELEGRAM_TOOL_COMMANDS:
        tool_name, parser, formatter = _TELEGRAM_TOOL_COMMANDS[cmd]
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
    memory.current_scope.set(_telegram_scope_for(chat_id))
    messages = _get_telegram_session(chat_id)

    parsed = _parse_command(text)
    if parsed is not None:
        cmd, args = parsed
        await _telegram_dispatch_command(update, cmd, args, context.bot)
        return

    if chat_id in _telegram_onboarding:
        await _telegram_handle_onboarding_answer(update, chat_id, text)
        return

    _refresh_system_prompt(messages, _telegram_scope_for(chat_id))
    messages.append({"role": "user", "content": text})
    loop = asyncio.get_running_loop()
    try:
        async with _typing(chat_id, context.bot):
            reply = await loop.run_in_executor(None, lambda: chat(messages))
    except Exception as e:
        messages.pop()
        await _tg_reply(update, f"Error: {e}")
        return
    messages.append({"role": "assistant", "content": reply})
    _, clean_reply = _split_thinking(reply)
    await _tg_reply(update, clean_reply)


def _make_telegram_cmd_handler(cmd: str):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id
        memory.current_scope.set(_telegram_scope_for(chat_id))
        _get_telegram_session(chat_id)
        await _telegram_dispatch_command(
            update, cmd, " ".join(context.args or []), context.bot
        )
    return handler


async def _start_telegram(token: str) -> Application:
    global _known_chats
    _known_chats = _load_known_chats()

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


async def _stop_telegram(app: Application) -> None:
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


# --- Entrypoint ---


async def _run_cli_repl() -> None:
    global _prompt_session, _cli_messages
    memory.current_scope.set("cli")
    channels.register(_CLIChannel())

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    hist = SQLiteHistory(session_id)
    _prompt_session = PromptSession(history=hist)

    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt("cli", now),
        },
    ]
    _cli_messages = messages

    cfg = get_config()
    if cfg.provider == "ollama":
        model_label = cfg.model
    elif cfg.provider == "openai":
        model_label = cfg.openai_model
    else:
        model_label = cfg.anthropic_model
    console.print(
        f"metalclaw bot ({cfg.provider}: {model_label}) — type 'quit' to exit"
    )
    if cfg.escalation_enabled and cfg.provider == "ollama":
        console.print(
            f"[dim]escalation: {cfg.escalation_provider}: {cfg.escalation_model}[/dim]"
        )
    console.print(f"  {len(TOOLS)} tool(s) loaded: {', '.join(TOOLS)}\n")

    with patch_stdout(raw=True):
        while True:
            try:
                user_input = (await _prompt_session.prompt_async("you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not user_input or user_input.lower() in ("quit", "exit"):
                break

            parsed = _parse_command(user_input)
            if parsed is not None:
                cmd, args = parsed
                handler = _COMMAND_HANDLERS.get(cmd)
                if handler is None:
                    console.print(f"unknown command: /{cmd}  (try /help)")
                    continue
                try:
                    handler(args)
                except ValueError as e:
                    console.print(str(e))
                continue

            if _pending_onboarding:
                key, _question = _pending_onboarding.pop(0)
                if user_input != "-":
                    value = _format_interests(user_input) if key == "interests" else user_input
                    memory.set_preference(key, value)
                if _pending_onboarding:
                    _, next_q = _pending_onboarding[0]
                    console.print(f"[dim]{next_q}[/dim]")
                else:
                    console.print("[dim]done — restart for memory to enter the system prompt[/dim]")
                continue

            _refresh_system_prompt(messages, "cli")
            messages.append({"role": "user", "content": user_input})

            try:
                with console.status("[dim]thinking…[/dim]", spinner="dots"):
                    reply = await asyncio.get_running_loop().run_in_executor(
                        None, lambda: chat(messages, on_tool_call=_cli_tool_log)
                    )
            except Exception as e:
                console.print(f"\n[bold]bot>[/bold] Error: {e}\n")
                messages.pop()
                continue

            messages.append({"role": "assistant", "content": reply})
            hist.save_assistant(reply)

            thinking, clean_reply = _split_thinking(reply)
            if thinking and _show_thinking:
                console.print(f"\n[dim]{thinking}[/dim]")
            _print_bot_markdown(clean_reply)


async def _async_main(*, daemon: bool, with_telegram: bool) -> None:
    import tools  # noqa: F401 — triggers @tool registrations

    cfg = get_config()
    tg_app: Application | None = None
    if with_telegram:
        if not cfg.telegram_bot_token:
            if daemon:
                raise RuntimeError(
                    "telegram_bot_token missing — set TELEGRAM_BOT_TOKEN env or "
                    "telegram_bot_token in config.yaml"
                )
            console.print("[dim]no telegram_bot_token — telegram disabled[/dim]")
        else:
            tg_app = await _start_telegram(cfg.telegram_bot_token)
            console.print(f"[dim]telegram polling started ({len(_known_chats)} known chat(s))[/dim]")

    stop = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat.run(stop))

    try:
        if daemon:
            console.print("[dim]daemon mode — Ctrl-C to exit[/dim]")
            try:
                await asyncio.Event().wait()
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
        else:
            await _run_cli_repl()
    finally:
        stop.set()
        try:
            await asyncio.wait_for(hb_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            hb_task.cancel()
        if tg_app is not None:
            await _stop_telegram(tg_app)


def main() -> None:
    parser = argparse.ArgumentParser(prog="metalclaw")
    parser.add_argument(
        "--daemon", action="store_true",
        help="run without CLI REPL (Telegram + heartbeat only)",
    )
    parser.add_argument(
        "--no-telegram", action="store_true",
        help="skip starting the Telegram frontend",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.daemon else logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)

    try:
        asyncio.run(_async_main(daemon=args.daemon, with_telegram=not args.no_telegram))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
