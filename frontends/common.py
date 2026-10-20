"""Frontend-agnostic helpers shared by CLI, Telegram, and Discord.

Currently exposes:
- heartbeat-scope-string helpers (used by ``heartbeat.py`` to route alerts back
  to the right frontend; memory itself has no scope)
- argparse-based parsers and plain-string formatters for the tool slash commands
  (``/train``, ``/weather``, ``/mail``, ``/search``, ``/wilma``) and a ``TOOL_COMMANDS``
  registry mapping slash name → ``(tool_name, parser, formatter)``

The ``run_*`` slash-command helpers (remember/forget/memory/heartbeat/big) are
added later by callers — they take ``send`` callbacks so each frontend keeps
its own reply mechanism.
"""

import argparse
import asyncio
import collections
import logging
import shlex
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

import channels
import heartbeat
import history
import live_tool
import mcp_client
import memory
from chat_loop import chat_via_escalation, run_turn, scoped_chat
from config import get_config

log = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]

REPO_ROOT = Path(__file__).resolve().parent.parent

TELEGRAM_SCOPE_PREFIX = "telegram-"
DISCORD_SCOPE_PREFIX = "discord-"

_pending_self_change: dict[str, live_tool.LiveAddState] = {}


# --- Cross-frontend slash-command registry ---
#
# Single source of truth for command name + descriptions. Drives:
#   - HELP_LINES (the /help output, identical across frontends)
#   - Telegram BotCommand registration (telegram_bot_commands())
#   - CLI completer + handler map (cli_command_table() + canonicalize())
#   - Discord dispatch (canonicalize())
#
# Adding a command here, plus a dispatch branch in the relevant frontend(s),
# is the only place new command metadata needs to land.

CLI = "cli"
TELEGRAM = "telegram"
DISCORD = "discord"
ALL_FRONTENDS = frozenset({CLI, TELEGRAM, DISCORD})


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
        "/add-tool <description> — write a new tool live (CLI/Telegram/Discord)",
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
        "self-edit",
        "/self-edit <description> — make a general code change (CLI only)",
        "make a general code change (full lint/build/test)",
        frozenset({CLI}),
    ),
    CommandSpec(
        "think",
        "/think — toggle display of model thinking (off by default)",
        "toggle display of model thinking (off by default)",
        frozenset({CLI}),
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


def cli_command_table() -> dict[str, str]:
    """Mapping ``name → short`` for the CLI completer."""
    return {s.name: s.short for s in COMMANDS if CLI in s.frontends}


HELP_LINES: list[str] = help_lines()


def telegram_scope(chat_id: int) -> str:
    return f"{TELEGRAM_SCOPE_PREFIX}{chat_id}"


def discord_scope(channel_id: int) -> str:
    return f"{DISCORD_SCOPE_PREFIX}{channel_id}"


def parse_telegram_scope(scope: str) -> int | None:
    if not scope.startswith(TELEGRAM_SCOPE_PREFIX):
        return None
    try:
        return int(scope[len(TELEGRAM_SCOPE_PREFIX) :])
    except ValueError:
        return None


def parse_discord_scope(scope: str) -> int | None:
    if not scope.startswith(DISCORD_SCOPE_PREFIX):
        return None
    try:
        return int(scope[len(DISCORD_SCOPE_PREFIX) :])
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


# --- Shared slash-command runners (Telegram + Discord) ---


async def run_remember(send: SendFn, args: str) -> None:
    log.info("/remember args=%.200r", args)
    if "=" not in args:
        await send("usage: /remember <key>=<value>")
        return
    key, value = args.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key or not value:
        await send("usage: /remember <key>=<value>")
        return
    memory.set_preference(key, value)
    log.info("/remember saved key=%r", key)
    await send(f"saved {key}={value}")


async def run_forget(send: SendFn, args: str) -> None:
    matcher = args.strip()
    log.info("/forget matcher=%r", matcher)
    if not matcher:
        await send("usage: /forget <substring>")
        return
    res = memory.forget(matcher)
    log.info("/forget status=%s matches=%d", res.status, len(res.matches or []))
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
    log.info("/memory")
    await send(memory.render_full())


async def run_manual(send: SendFn, args: str) -> None:
    """Drive the read_manual tool from a slash command.

    ``/manual``           → table of contents.
    ``/manual <section>`` → that section's prose.
    ``/manual init``      → copy the bundled template into the user's vault.
    """
    from tools import manual as manual_tool

    arg = args.strip()
    log.info("/manual arg=%r", arg)
    if arg == "init":
        res = manual_tool.init_manual()
        if res["status"] == "created":
            await send(f"manual created at {res['path']}")
        else:
            await send(f"manual already exists at {res['path']}")
        return

    res = manual_tool.read_manual(arg or None)
    if "error" in res:
        if res["error"] == "manual_not_initialised":
            await send(f"manual not initialised — {res['hint']}")
            return
        available = ", ".join(res.get("available", []))
        await send(
            f"unknown manual section '{res['requested']}'. available: {available}"
        )
        return

    if "markdown" in res:
        await send(res["markdown"])
        return

    body = res["toc"] + "\n" + res["hint"]
    await send(body)


def _format_action_line(action: heartbeat.HeartbeatAction) -> str:
    if action.kind == heartbeat.ActionKind.AT:
        when = f"at {action.at}"
    elif action.kind == heartbeat.ActionKind.CRON and action.schedule is not None:
        when = (
            f"{','.join(action.schedule.days)} {action.schedule.time} "
            f"({action.schedule.timezone})"
        )
    elif action.kind == heartbeat.ActionKind.EVERY:
        when = f"every {action.every}s"
    else:
        when = "(unscheduled)"
    return (
        f"  • {action.id} [{action.kind}] → {action.channel} | {when} | {action.prompt}"
    )


async def run_heartbeat(
    send: SendFn, scope: str, sub: str, *, warn_no_discord_channel: bool = False
) -> None:
    log.info("/heartbeat sub=%r scope=%s", sub, scope)
    if sub == "run":
        if heartbeat.spawn_tick() is None:
            await send("heartbeat tick already running")
        else:
            await send("heartbeat tick fired")
        return

    cfg = get_config()
    lines = [
        (
            f"heartbeat enabled={cfg.heartbeat_enabled} "
            f"interval={cfg.heartbeat_interval_seconds}s "
            f"default_channel={cfg.heartbeat_default_channel or '(unset)'}"
        ),
    ]
    if warn_no_discord_channel and cfg.discord_heartbeat_channel is None:
        lines.append(
            "(no discord_heartbeat_channel configured — discord replies will drop)"
        )

    try:
        ledger = heartbeat.load_ledger()
    except Exception as e:
        log.exception("/heartbeat ledger read failed")
        await send("\n".join([*lines, f"ledger read failed: {e}"]))
        return

    if ledger.actions:
        lines.append("active actions:")
        lines.extend(_format_action_line(a) for a in ledger.actions)
    else:
        lines.append("no active actions — ask the bot to schedule one")

    if ledger.completed:
        recent = ledger.completed[-5:]
        lines.append(f"recent completed (last {len(recent)}):")
        for c in recent:
            done_at = c.get("completed_at", "?")
            kind = c.get("kind", "?")
            cid = c.get("id", "?")
            prompt = str(c.get("prompt", ""))[:60]
            lines.append(f"  • {cid} [{kind}] @ {done_at} | {prompt}")

    await send("\n".join(lines))


async def run_big(
    send: SendFn,
    typing_ctx,
    messages: list[dict],
    query: str,
    scope: str | None = None,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
) -> None:
    """Run an escalation turn. ``typing_ctx`` is an async context manager
    (e.g. Telegram's typing pulser, Discord's ``channel.typing()``, or
    ``contextlib.nullcontext()``) shown while the call blocks. ``scope``
    pins ``chat_loop.current_scope`` for the duration of the turn so any
    tool the big model calls can resolve the active surface."""
    log.info("/big scope=%s query=%d chars", scope, len(query))
    if not query:
        await send("usage: /big <query>")
        return
    cfg = get_config()
    if not cfg.escalation_enabled:
        log.warning("/big refused: escalation disabled")
        await send("escalation disabled — set escalation_enabled: true in config.yaml")
        return
    try:
        async with typing_ctx:
            _, _, clean_reply = await run_turn(
                messages,
                query,
                lambda: scoped_chat(
                    scope,
                    lambda: chat_via_escalation(messages, on_tool_call=on_tool_call),
                ),
            )
    except Exception as e:
        log.exception("/big failed")
        await send(f"Error: {e}")
        return
    log.info("/big reply %d chars", len(clean_reply))
    if isinstance(on_tool_call, ToolStatusReporter):
        await on_tool_call.drain()
    await send(clean_reply)


# --- /status and /usage ---

_STARTED = time.monotonic()


class RecentWarnings(logging.Handler):
    """In-memory ring buffer of the last WARNING+ records, for /status."""

    def __init__(self, maxlen: int = 5) -> None:
        super().__init__(level=logging.WARNING)
        self.records: collections.deque[str] = collections.deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        self.records.append(
            f"{ts} {record.levelname} {record.name}: {record.getMessage()}"
        )


recent_warnings = RecentWarnings()


def _fmt_td(td: timedelta) -> str:
    mins = int(td.total_seconds()) // 60
    d, rem = divmod(mins, 1440)
    h, m = divmod(rem, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def format_status() -> str:
    cfg = get_config()
    model = cfg.model if cfg.provider == "ollama" else cfg.litellm_model
    lines = [
        f"provider: {cfg.provider} / {model}",
        (
            f"escalation: {cfg.escalation_provider} / {cfg.escalation_model}"
            if cfg.escalation_enabled
            else "escalation: disabled"
        ),
        f"uptime: {_fmt_td(timedelta(seconds=time.monotonic() - _STARTED))}",
    ]
    active = {
        name: sorted(ch.active_scopes()) for name, ch in channels.CHANNELS.items()
    }
    lines.append(
        "frontends: "
        + (
            "; ".join(f"{n} ({len(sc)} active)" for n, sc in sorted(active.items()))
            or "none registered"
        )
    )
    try:
        actions = heartbeat.list_active()
        state = heartbeat.load_state()
    except Exception as e:
        log.exception("/status: heartbeat read failed")
        actions, state = [], {}
        lines.append(f"heartbeat: ledger read failed: {e}")
    else:
        lines.append(
            f"heartbeat: enabled={cfg.heartbeat_enabled} "
            f"interval={cfg.heartbeat_interval_seconds}s actions={len(actions)}"
        )
    now = datetime.now(UTC)
    for a in actions:
        st = state.get(a.id)
        nxt = heartbeat.next_due(a, st.last_run if st else None, now)
        retry = st.retry_at() if st else None
        if nxt is not None and retry is not None and retry > nxt:
            nxt = retry
        when = "never" if nxt is None else nxt.astimezone().strftime("%Y-%m-%d %H:%M")
        suffix = ""
        if st is not None and st.failure_count > 0:
            plural = "s" if st.failure_count != 1 else ""
            suffix = f" ({st.failure_count} failure{plural}, backing off)"
        lines.append(f"  • {a.id} [{a.kind}] next: {when}{suffix} | {a.prompt[:50]}")
    servers = mcp_client.status()
    if servers:
        lines.append(f"mcp: {len(servers)} configured")
        for m in servers:
            line = f"  • {m['name']} {m['status']} {m['transport']} {len(m['tools'])} tools"
            if m["tools"]:
                line += f" ({', '.join(m['tools'])})"
            if m["error"]:
                line += f" — {m['error'][:120]}"
            lines.append(line)
    recent = list(recent_warnings.records)
    lines.append(
        f"recent warnings/errors (last {len(recent)}):"
        if recent
        else "recent warnings/errors: none"
    )
    lines.extend(f"  {r[:200]}" for r in recent)
    return "\n".join(lines)


def _usage_block(title: str, since: datetime) -> list[str]:
    rows = history.usage_since(since.astimezone(UTC).isoformat())
    if not rows:
        return [f"{title}: no calls recorded"]
    out = [f"{title}:"]
    for provider, model, calls, prompt, completion, cost, known in rows:
        if known == 0:
            cost_s = "cost n/a"
        else:
            cost_s = f"${cost:.4f}" + ("" if known == calls else " (partial)")
        out.append(
            f"  • {provider}/{model}: {calls} call(s), {prompt} in / {completion} out, {cost_s}"
        )
    return out


def format_usage() -> str:
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return "\n".join(
        [
            *_usage_block("today", midnight),
            *_usage_block("last 30 days", now - timedelta(days=30)),
        ]
    )


async def run_status(send: SendFn) -> None:
    await send(format_status())


async def run_usage(send: SendFn) -> None:
    await send(format_usage())


# --- message debounce (Telegram / Discord) ---


class Debouncer:
    """Per-key quiet-period buffer.

    ``add`` appends text and restarts the key's timer; after ``delay`` seconds
    with no further ``add``, ``flush(key, joined_text, latest_ctx)`` runs once.
    The timer entry is dropped before ``flush`` starts, so a message arriving
    mid-turn starts a fresh timer instead of cancelling the running turn; the
    frontends' per-session lock then serialises the two turns.
    """

    def __init__(self, flush: Callable[[Any, str, Any], Awaitable[None]]) -> None:
        self._flush = flush
        self._pending: dict[Hashable, tuple[list[str], Any]] = {}
        self._timers: dict[Hashable, asyncio.Task] = {}

    def add(self, key: Hashable, text: str, ctx: Any, delay: float) -> None:
        texts = self._pending.get(key, ([], None))[0]
        texts.append(text)
        self._pending[key] = (texts, ctx)
        if (old := self._timers.get(key)) is not None:
            old.cancel()
        self._timers[key] = asyncio.create_task(self._fire(key, delay))
        log.debug("debounce: key=%s buffered=%d delay=%.2fs", key, len(texts), delay)

    async def _fire(self, key: Hashable, delay: float) -> None:
        await asyncio.sleep(delay)
        texts, ctx = self._pending.pop(key)
        del self._timers[key]
        log.info("debounce: flushing key=%s messages=%d", key, len(texts))
        try:
            await self._flush(key, "\n".join(texts), ctx)
        except Exception:
            log.exception("debounce: flush failed key=%s", key)


# --- live tool-call status ---

_TOOL_EMOJI_EXACT = {
    "mark_emails_reported": "✅",
    "read_email": "📖",
    "read_note": "📖",
    "search_vault": "🔍",
    "weather": "🌦️",
    "get_weather": "🌦️",
    "train_departures": "🚆",
    "roll_die": "🎲",
    "escalate_to_big_model": "🚀",
}
# first match wins
_TOOL_EMOJI_PREFIX = (
    ("add_email_label", "🏷️"),
    ("remove_email_label", "🏷️"),
    ("list_email_label", "🏷️"),
    ("email_label", "🏷️"),
    ("check_new_mail", "📧"),
    ("list_email", "📧"),
    ("weather", "🌦️"),
    ("manual", "📘"),
    ("get_manual", "📘"),
    ("read_manual", "📘"),
    ("set_user_", "🧠"),
    ("add_user_", "🧠"),
    ("forget_user_", "🧠"),
    ("get_user_", "🧠"),
    ("create_heartbeat", "⏰"),
    ("list_heartbeat", "⏰"),
    ("cancel_heartbeat", "⏰"),
)
_ARG_CHARS = 40
_LINE_CHARS = 100
TELEGRAM_STATUS_LIMIT = 4096
DISCORD_STATUS_LIMIT = 2000


def _tool_emoji(name: str) -> str:
    if name in _TOOL_EMOJI_EXACT:
        return _TOOL_EMOJI_EXACT[name]
    for prefix, emoji in _TOOL_EMOJI_PREFIX:
        if name.startswith(prefix):
            return emoji
    return "🔧"


def _clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _arg_value(v: object) -> str:
    if isinstance(v, (list, tuple, set)) and len(v) > 2:
        return f"{len(v)} items"
    if isinstance(v, str):
        return _clip(v, _ARG_CHARS)
    return _clip(str(v), _ARG_CHARS)


def format_tool_status(name: str, args: dict) -> str:
    """One plain-text line: emoji + tool name + short arg summary."""
    parts = []
    for k, v in (args or {}).items():
        val = _arg_value(v)
        if isinstance(v, str) and len(args) > 1:
            parts.append(f"{k}={val!r}")
        elif isinstance(v, str):
            parts.append(f'"{val}"' if " " in val or name == "search_vault" else val)
        elif len(args) > 1:
            parts.append(f"{k}={val}")
        else:
            parts.append(val)
    line = f"{_tool_emoji(name)} {name}"
    if parts:
        line += f"({_clip(', '.join(parts), _LINE_CHARS)})"
    return _clip(line, _LINE_CHARS + len(name) + 8)


class ToolStatusReporter:
    """Live tool-call status message. ``__call__`` matches ``on_tool_call`` and
    is invoked from the worker thread; it schedules send/edit on ``loop``.

    ``send(text) -> handle`` posts the first message, ``edit(handle, text)``
    updates it. Updates run under one asyncio.Lock (FIFO), so the initial send
    always completes before the first edit. All errors are logged and swallowed.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        send: Callable[[str], Awaitable[object]],
        edit: Callable[[object, str], Awaitable[object]],
        limit: int = TELEGRAM_STATUS_LIMIT,
    ) -> None:
        self._loop = loop
        self._send = send
        self._edit = edit
        self._limit = limit
        self._lines: list[str] = []
        self._handle: object = None
        self._lock = asyncio.Lock()
        self._pending: list = []

    def _render(self) -> str:
        lines = self._lines
        text = "\n".join(lines)
        if len(text) <= self._limit:
            return text
        kept: list[str] = []
        size = 2  # "…\n"
        for line in reversed(lines):
            if size + len(line) + 1 > self._limit:
                break
            kept.append(line)
            size += len(line) + 1
        return "…\n" + "\n".join(reversed(kept))

    async def _upsert(self, text: str) -> None:
        async with self._lock:
            try:
                if self._handle is None:
                    self._handle = await self._send(text)
                else:
                    await self._edit(self._handle, text)
            except Exception:
                log.exception("tool status update failed")

    def __call__(self, name: str, args: dict, result: str) -> None:
        try:
            self._lines.append(format_tool_status(name, args))
            fut = asyncio.run_coroutine_threadsafe(
                self._upsert(self._render()), self._loop
            )
            self._pending.append(fut)
        except Exception:
            log.exception("tool status schedule failed")

    async def drain(self) -> None:
        """Await scheduled updates (event loop) so the status lands before the reply."""
        pending, self._pending = self._pending, []
        for fut in pending:
            try:
                await asyncio.wrap_future(fut)
            except Exception:
                log.exception("tool status drain failed")

    def flush(self, timeout: float = 10.0) -> None:
        """Block (worker thread / tests) until scheduled updates finish."""
        for fut in self._pending:
            try:
                fut.result(timeout)
            except Exception:
                log.exception("tool status flush failed")
        self._pending.clear()


def parse_tools_arg(current: bool, arg: str) -> tuple[bool, str]:
    """Resolve ``/tools [on|off]`` -> (new_state, reply). No arg shows state."""
    a = arg.strip().lower()
    if a in ("on", "off"):
        new = a == "on"
    elif a == "":
        return (
            current,
            f"tool-call status is {'on' if current else 'off'} (/tools on|off)",
        )
    else:
        return current, "usage: /tools [on|off]"
    return new, f"tool-call status {'on' if new else 'off'}"


# --- self-change pending-state pump ---


def _format_self_change_summary(state: live_tool.LiveAddState) -> str:
    lines = [f"self-change ready: {state.slug}"]
    lines.append(f"  file: {state.new_file}")
    registered = ", ".join(state.registered_names) or "(none — gate failed)"
    lines.append(f"  registered: {registered}")
    for gate in ("ruff", "import", "schema"):
        if gate not in state.gate_results:
            continue
        marker = "ok" if state.gate_results[gate] else "FAIL"
        msg = state.gate_messages.get(gate, "")
        lines.append(f"  {gate}: {marker} — {msg}" if msg else f"  {gate}: {marker}")
    lines.append("commands: /approve  /approve_force  /reject  /diff")
    return "\n".join(lines)


async def run_add_tool(send: SendFn, request: str, scope: str) -> None:
    log.info("/add-tool scope=%s request=%.200r", scope, request)
    if not request.strip():
        await send("usage: /add-tool <description of the tool to add>")
        return
    if not get_config().allow_self_modification:
        log.warning(
            "/add-tool refused: allow_self_modification=false (scope=%s)", scope
        )
        await send(
            "self-modification is disabled (allow_self_modification: false in config.yaml)"
        )
        return
    if scope in _pending_self_change:
        log.info("/add-tool refused: self-change already pending for %s", scope)
        await send(
            "a self-change is already pending — /approve, /approve_force, /reject, or /diff first"
        )
        return
    await send("running self-change… this can take a few minutes.")
    state = await asyncio.to_thread(live_tool.run_add_tool_live, request, REPO_ROOT)
    if state.aborted:
        log.warning("/add-tool aborted: %s", state.aborted)
        await send(f"aborted: {state.aborted}\n\n{state.plan_output}".strip())
        return
    _pending_self_change[scope] = state
    log.info(
        "/add-tool pending scope=%s slug=%s gates=%s",
        scope,
        state.slug,
        state.gate_results,
    )
    await send(_format_self_change_summary(state))


async def run_approve(send: SendFn, scope: str, *, force: bool = False) -> None:
    state = _pending_self_change.get(scope)
    log.info("/approve force=%s scope=%s pending=%s", force, scope, state is not None)
    if state is None:
        await send("no pending self-change")
        return
    res = live_tool.finalise_add_tool_live(
        state, "approve-force" if force else "approve"
    )
    if res.ok:
        _pending_self_change.pop(scope, None)
    await send(res.message)


async def run_reject(send: SendFn, scope: str) -> None:
    state = _pending_self_change.get(scope)
    log.info("/reject scope=%s pending=%s", scope, state is not None)
    if state is None:
        await send("no pending self-change")
        return
    res = live_tool.finalise_add_tool_live(state, "reject")
    if res.ok:
        _pending_self_change.pop(scope, None)
    await send(res.message)


async def run_diff(send: SendFn, scope: str) -> None:
    state = _pending_self_change.get(scope)
    log.info("/diff scope=%s pending=%s", scope, state is not None)
    if state is None:
        await send("no pending self-change")
        return
    res = live_tool.finalise_add_tool_live(state, "diff")
    body = res.diff or res.message
    await send(f"```\n{body}\n```")


def has_pending_self_change(scope: str) -> bool:
    return scope in _pending_self_change
