"""Cross-frontend slash-command metadata and the tool slash commands.

- ``COMMANDS`` spec table: single source of the ``/help`` lines, the Telegram
  ``BotCommand`` list and ``canonicalize()``
- argparse-based parsers and plain-string formatters for the tool slash commands
  (``/train``, ``/weather``, ``/mail``, ``/search``, ``/wilma``) and a ``TOOL_COMMANDS``
  registry mapping slash name -> ``(tool_name, parser, formatter)``
"""

import argparse
import shlex
from dataclasses import dataclass, field
from typing import NoReturn

# --- Cross-frontend slash-command registry ---
#
# Single source of truth for command name + descriptions. Drives:
#   - HELP_LINES (the /help output, identical across frontends)
#   - Telegram BotCommand registration (telegram_bot_commands())
#   - Discord dispatch (canonicalize())
#
# Adding a command here, plus a dispatch branch in the relevant frontend(s),
# is the only place new command metadata needs to land.

TELEGRAM = "telegram"
DISCORD = "discord"
ALL_FRONTENDS = frozenset({TELEGRAM, DISCORD})


@dataclass(frozen=True)
class CommandSpec:
    name: str  # canonical hyphenated form, e.g. "add-tool"
    help_line: str  # full /help line including arg syntax
    short: str  # 1-line description for completer / Telegram BotCommand
    frontends: frozenset[str]
    aliases: tuple[str, ...] = field(default=())  # extra spellings users may type


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec(
        "train",
        "/train <station> [--line R] [--count 5]",
        "train departures: <station> [--line R] [--count 5]",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "weather", "/weather <location>", "weather for a location", ALL_FRONTENDS
    ),
    CommandSpec(
        "mail",
        "/mail [--mailbox inbox] [--unread] [--from name] [--count 10]",
        "list emails [--mailbox] [--unread] [--from] [--count]",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "wilma",
        "/wilma [--student name] [--unread] [--count 10] — list Wilma messages",
        "list Wilma messages [--student] [--unread] [--count]",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "search",
        "/search <query> [--max 20] [--context 1] — search the Obsidian vault",
        "search the Obsidian vault",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "remember",
        "/remember <key>=<value> — save a preference",
        "save a preference: <key>=<value>",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "forget",
        "/forget <substring> — remove a memory entry",
        "remove a memory entry",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "memory",
        "/memory — show stored memory",
        "show stored long-term memory",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "manual",
        "/manual [section] — show the user manual; /manual init to create it",
        "show the user manual ('init' to create)",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "heartbeat",
        "/heartbeat — show heartbeat config; /heartbeat run to fire now",
        "show heartbeat config (or 'run' to fire now)",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "big",
        "/big <query> — ask the escalation cloud model directly",
        "ask the escalation cloud model directly",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "add-tool",
        "/add-tool <description> — write a new tool live",
        "add a new tool live: <description>",
        ALL_FRONTENDS,
        aliases=("add_tool", "addtool"),
    ),
    CommandSpec(
        "approve",
        "/approve — accept a pending self-change",
        "approve a pending self-change",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "approve-force",
        "/approve-force — accept a pending self-change despite failing gates",
        "approve a pending self-change despite failing gates",
        ALL_FRONTENDS,
        aliases=("approve_force",),
    ),
    CommandSpec(
        "reject",
        "/reject — discard a pending self-change",
        "reject a pending self-change",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "diff",
        "/diff — show diff of a pending self-change",
        "show diff of a pending self-change",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "status",
        "/status — model, uptime, frontends, heartbeat schedule, recent warnings",
        "show bot status",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "usage",
        "/usage — LLM token usage and cost (today, last 30 days)",
        "show LLM token usage and cost",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "tools",
        "/tools [on|off] — show/toggle live tool-call status",
        "show/toggle live tool-call status: [on|off]",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "new",
        "/new — reset this conversation",
        "reset this conversation",
        ALL_FRONTENDS,
    ),
    CommandSpec(
        "help", "/help — this message", "show available commands", ALL_FRONTENDS
    ),
)


def _build_name_index() -> dict[str, CommandSpec]:
    out: dict[str, CommandSpec] = {}
    for spec in COMMANDS:
        out[spec.name] = spec
        for alias in spec.aliases:
            out[alias] = spec
    return out


_BY_ANY_NAME: dict[str, CommandSpec] = _build_name_index()


def canonicalize(cmd: str) -> str | None:
    """Map a user-typed command name (or alias) to its canonical form, else None.

    Strips a leading slash and lowercases. Returns ``None`` for unknown
    commands so callers can decide whether to error or fall through (e.g. to
    ``TOOL_COMMANDS`` lookup).
    """
    spec = _BY_ANY_NAME.get(cmd.lstrip("/").lower())
    return spec.name if spec else None


def help_lines() -> list[str]:
    """Lines for /help across all frontends — built from COMMANDS."""
    return ["Available commands:", *[s.help_line for s in COMMANDS]]


def telegram_bot_commands() -> list[tuple[str, str]]:
    """List for ``Application.bot.set_my_commands`` — Telegram needs ``[a-z][a-z0-9_]*`` so hyphens become underscores."""
    return [
        (s.name.replace("-", "_"), s.short) for s in COMMANDS if TELEGRAM in s.frontends
    ]


HELP_LINES: list[str] = help_lines()


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
    return {
        "mailbox": ns.mailbox,
        "unread_only": ns.unread,
        "from_search": ns.from_search,
        "limit": ns.count,
    }


def parse_wilma_args(args: str) -> dict:
    p = _ArgParser(prog="/wilma", add_help=False)
    p.add_argument("--student", default=None)
    p.add_argument("--folder", default="inbox", choices=["inbox", "archive", "outbox"])
    p.add_argument("--unread", action="store_true")
    p.add_argument("--count", type=int, default=10)
    ns = p.parse_args(shlex.split(args))
    return {
        "student": ns.student,
        "folder": ns.folder,
        "unread_only": ns.unread,
        "limit": ns.count,
    }


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
        return (
            f"No upcoming departures found for {station['name']} ({station['code']})."
        )

    lines = [f"{header}:"]
    for dep in departures:
        name = dep["line"] or f"{dep['train_type']} {dep['train_number']}".strip()
        if dep["line"] and not line:
            name += f" ({dep['train_type']} {dep['train_number']})"

        scheduled = dep["scheduled_time"][11:16]
        time_str = scheduled
        if dep.get("actual_time") and dep["actual_time"] != dep["scheduled_time"]:
            time_str += f" (actual {dep['actual_time'][11:16]})"
        elif (
            dep.get("estimated_time") and dep["estimated_time"] != dep["scheduled_time"]
        ):
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


def format_wilma_result(result: dict) -> str:
    blocks = []
    for s in result.get("students", []):
        header = f"**{s.get('student_name') or s.get('student') or 'Wilma'}** — {result.get('folder', 'inbox')}"
        msgs = s.get("messages", [])
        if not msgs:
            blocks.append(f"{header}\n\nNo messages.")
            continue
        lines = [header, ""]
        for m in msgs:
            unread_tag = " [UNREAD]" if m.get("unread") else ""
            sent = (m.get("sent_at") or "")[:16].replace("T", " ")
            lines.append(f"- `{m['id']}` {sent}  {m.get('sender') or '?'}{unread_tag}")
            lines.append(f"  {m['subject']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) or "No Wilma students found."


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
    "train": ("train_departures", parse_train_args, format_train_result),
    "weather": ("weather", parse_weather_args, format_weather_result),
    "mail": ("list_emails", parse_mail_args, format_mail_result),
    "search": ("search_vault", parse_search_args, format_search_result),
    "wilma": ("list_wilma_messages", parse_wilma_args, format_wilma_result),
}
