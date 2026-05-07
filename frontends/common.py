"""Frontend-agnostic helpers shared by CLI, Telegram, and Discord.

Currently exposes:
- heartbeat-scope-string helpers (used by ``heartbeat.py`` to route alerts back
  to the right frontend; memory itself has no scope)
- argparse-based parsers and plain-string formatters for the tool slash commands
  (``/train``, ``/weather``, ``/mail``, ``/search``) and a ``TOOL_COMMANDS``
  registry mapping slash name → ``(tool_name, parser, formatter)``

The ``run_*`` slash-command helpers (remember/forget/memory/heartbeat/big) are
added later by callers — they take ``send`` callbacks so each frontend keeps
its own reply mechanism.
"""

import argparse
import asyncio
import shlex
from collections.abc import Awaitable, Callable
from typing import NoReturn

import heartbeat
import memory
from chat_loop import _refresh_system_prompt, _split_thinking, chat_via_escalation
from config import get_config

SendFn = Callable[[str], Awaitable[None]]

TELEGRAM_SCOPE_PREFIX = "telegram-"
DISCORD_SCOPE_PREFIX = "discord-"


HELP_LINES: list[str] = [
    "Available commands:",
    "/train <station> [--line R] [--count 5]",
    "/weather <location>",
    "/mail [--mailbox inbox] [--unread] [--from name] [--count 10]",
    "/search <query> [--max 20] [--context 1] — search the Obsidian vault",
    "/remember <key>=<value> — save a preference",
    "/forget <substring> — remove a memory entry",
    "/memory — show stored memory",
    "/heartbeat — show heartbeat config; /heartbeat run to fire now",
    "/big <query> — ask the escalation cloud model directly",
    "/new — reset this conversation",
    "/help — this message",
]


def telegram_scope(chat_id: int) -> str:
    return f"{TELEGRAM_SCOPE_PREFIX}{chat_id}"


def discord_scope(channel_id: int) -> str:
    return f"{DISCORD_SCOPE_PREFIX}{channel_id}"


def parse_telegram_scope(scope: str) -> int | None:
    if not scope.startswith(TELEGRAM_SCOPE_PREFIX):
        return None
    try:
        return int(scope[len(TELEGRAM_SCOPE_PREFIX):])
    except ValueError:
        return None


def parse_discord_scope(scope: str) -> int | None:
    if not scope.startswith(DISCORD_SCOPE_PREFIX):
        return None
    try:
        return int(scope[len(DISCORD_SCOPE_PREFIX):])
    except ValueError:
        return None


# --- Tool slash commands (parsers + formatters) ---


class _ArgParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ValueError(message)


def parse_train_args(args: str) -> dict:
    p = _ArgParser(prog="/train", add_help=False)
    p.add_argument("station")
    p.add_argument("--line", default=None)
    p.add_argument("--count", type=int, default=5)
    ns = p.parse_args(shlex.split(args))
    return {"station": ns.station, "line": ns.line, "count": ns.count}


def parse_weather_args(args: str) -> dict:
    try:
        parts = shlex.split(args)
    except ValueError as e:
        raise ValueError(f"invalid /weather arguments: {e}") from e
    if not parts:
        raise ValueError("usage: /weather <location>")
    return {"location": " ".join(parts)}


def parse_mail_args(args: str) -> dict:
    p = _ArgParser(prog="/mail", add_help=False)
    p.add_argument("--mailbox", default="inbox")
    p.add_argument("--unread", action="store_true")
    p.add_argument("--from", dest="from_search", default=None)
    p.add_argument("--count", type=int, default=10)
    ns = p.parse_args(shlex.split(args))
    return {"mailbox": ns.mailbox, "unread_only": ns.unread, "from_search": ns.from_search, "limit": ns.count}


def parse_search_args(args: str) -> dict:
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


def format_weather_result(result: dict) -> str:
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


def format_train_result(result: dict) -> str:
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


def format_mail_result(result: dict) -> str:
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


def format_search_result(result: dict) -> str:
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


# slash-command name -> (tool registry name, parser, formatter)
TOOL_COMMANDS: dict[str, tuple] = {
    "train":   ("train_departures", parse_train_args,   format_train_result),
    "weather": ("weather",          parse_weather_args, format_weather_result),
    "mail":    ("list_emails",      parse_mail_args,    format_mail_result),
    "search":  ("search_vault",     parse_search_args,  format_search_result),
}


# --- Shared slash-command runners (Telegram + Discord) ---


async def run_remember(send: SendFn, args: str) -> None:
    if "=" not in args:
        await send("usage: /remember <key>=<value>")
        return
    key, value = args.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key or not value:
        await send("usage: /remember <key>=<value>")
        return
    memory.set_preference(key, value)
    await send(f"saved {key}={value}")


async def run_forget(send: SendFn, args: str) -> None:
    matcher = args.strip()
    if not matcher:
        await send("usage: /forget <substring>")
        return
    res = memory.forget(matcher)
    if res.status == "removed":
        await send(f"forgot: {res.entry}")
    elif res.status == "ambiguous":
        lines = [f"'{matcher}' matches {len(res.matches)} entries:"]
        lines.extend(f"  {i}. {m}" for i, m in enumerate(res.matches, 1))
        lines.append("forget is final — refine matcher to hit exactly one entry.")
        await send("\n".join(lines))
    else:
        await send(f"no entry matched '{matcher}'")


async def run_memory(send: SendFn) -> None:
    await send(memory.render_full())


async def run_heartbeat(
    send: SendFn, scope: str, sub: str, *, warn_no_discord_channel: bool = False
) -> None:
    path = heartbeat.heartbeat_path_for(scope)
    if sub == "run":
        asyncio.create_task(heartbeat.run_tick())
        await send("heartbeat tick fired")
        return
    cfg = get_config()
    lines = [
        f"heartbeat enabled={cfg.heartbeat_enabled} interval={cfg.heartbeat_interval_seconds}s",
        f"checklist: {path}",
    ]
    if warn_no_discord_channel and cfg.discord_heartbeat_channel is None:
        lines.append("(no discord_heartbeat_channel configured — replies will drop)")
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
    await send("\n".join(lines))


async def run_big(
    send: SendFn,
    typing_ctx,
    messages: list[dict],
    query: str,
) -> None:
    """Run an escalation turn. ``typing_ctx`` is an async context manager
    (e.g. Telegram's typing pulser, Discord's ``channel.typing()``, or
    ``contextlib.nullcontext()``) shown while the call blocks."""
    if not query:
        await send("usage: /big <query>")
        return
    cfg = get_config()
    if not cfg.escalation_enabled:
        await send("escalation disabled — set escalation_enabled: true in config.yaml")
        return
    _refresh_system_prompt(messages)
    messages.append({"role": "user", "content": query})
    loop = asyncio.get_running_loop()
    try:
        async with typing_ctx:
            reply = await loop.run_in_executor(
                None, lambda: chat_via_escalation(messages)
            )
    except Exception as e:
        messages.pop()
        await send(f"Error: {e}")
        return
    _, clean_reply = _split_thinking(reply)
    await send(clean_reply)


