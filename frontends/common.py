"""Frontend-agnostic helpers shared by Telegram and Discord.

Exposes:
- the sender allowlist check (``is_allowed_user``)
- heartbeat-scope-string helpers (used by ``heartbeat.py`` to route alerts back
  to the right frontend; memory itself has no scope)
- the async ``run_*`` slash-command runners; they take ``send`` callbacks so
  each frontend keeps its own reply mechanism
- the message ``Debouncer`` and the ``/add-tool`` pending self-change state

Command metadata and tool slash commands live in ``frontends.commands``,
``/status`` / ``/usage`` formatting in ``frontends.status``, and live tool-call
status plus message splitting in ``frontends.tool_status``. Their public names
are re-exported here so ``common.X`` call sites keep working.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Hashable
from pathlib import Path
from typing import Any

import heartbeat
import live_tool
import memory
import self_change
from chat_loop import Session, chat_via_escalation, run_turn, scoped_chat
from config import get_config
from frontends.commands import (
    ALL_FRONTENDS,
    COMMANDS,
    DISCORD,
    HELP_LINES,
    TELEGRAM,
    TOOL_COMMANDS,
    CommandSpec,
    canonicalize,
    format_mail_result,
    format_search_result,
    format_train_result,
    format_weather_result,
    format_wilma_result,
    help_lines,
    parse_mail_args,
    parse_search_args,
    parse_train_args,
    parse_weather_args,
    parse_wilma_args,
    telegram_bot_commands,
)
from frontends.status import (
    RecentWarnings,
    format_status,
    format_usage,
    recent_warnings,
)
from frontends.tool_status import (
    DISCORD_STATUS_LIMIT,
    TELEGRAM_STATUS_LIMIT,
    ToolStatusReporter,
    format_tool_status,
    parse_tools_arg,
    split_message,
)

__all__ = [
    "ALL_FRONTENDS",
    "COMMANDS",
    "DISCORD",
    "DISCORD_SCOPE_PREFIX",
    "DISCORD_STATUS_LIMIT",
    "HELP_LINES",
    "REPO_ROOT",
    "TELEGRAM",
    "TELEGRAM_SCOPE_PREFIX",
    "TELEGRAM_STATUS_LIMIT",
    "TOOL_COMMANDS",
    "CommandSpec",
    "Debouncer",
    "RecentWarnings",
    "SendFn",
    "ToolStatusReporter",
    "canonicalize",
    "discord_scope",
    "format_mail_result",
    "format_search_result",
    "format_status",
    "format_tool_status",
    "format_train_result",
    "format_usage",
    "format_weather_result",
    "format_wilma_result",
    "has_pending_self_change",
    "help_lines",
    "is_allowed_user",
    "parse_discord_scope",
    "parse_mail_args",
    "parse_search_args",
    "parse_telegram_scope",
    "parse_tools_arg",
    "parse_train_args",
    "parse_weather_args",
    "parse_wilma_args",
    "recent_warnings",
    "run_add_tool",
    "run_approve",
    "run_big",
    "run_diff",
    "run_forget",
    "run_heartbeat",
    "run_manual",
    "run_memory",
    "run_reject",
    "run_remember",
    "run_status",
    "run_usage",
    "split_message",
    "telegram_bot_commands",
    "telegram_scope",
]

log = logging.getLogger(__name__)

SendFn = Callable[[str], Awaitable[None]]

REPO_ROOT = Path(__file__).resolve().parent.parent

TELEGRAM_SCOPE_PREFIX = "telegram-"
DISCORD_SCOPE_PREFIX = "discord-"

_pending_self_change: dict[str, live_tool.LiveAddState] = {}


def is_allowed_user(frontend: str, user_id: int | None) -> bool:
    """True if ``user_id`` is in the frontend's allowlist (empty = nobody)."""
    cfg = get_config()
    allowed = (
        cfg.telegram_allowed_users
        if frontend == TELEGRAM
        else cfg.discord_allowed_users
    )
    return user_id is not None and user_id in allowed


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
    session: Session,
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
                session,
                query,
                lambda: scoped_chat(
                    scope,
                    lambda: chat_via_escalation(session, on_tool_call=on_tool_call),
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
            "self-modification is disabled "
            "(set allow_self_modification: true in config.yaml)"
        )
        return
    if scope in _pending_self_change:
        log.info("/add-tool refused: self-change already pending for %s", scope)
        await send(
            "a self-change is already pending — /approve, /approve_force, /reject, or /diff first"
        )
        return
    if self_change.is_busy():
        # Early, friendly refusal; run_add_tool_live re-checks under the lock.
        log.info("/add-tool refused: another self-modification is running")
        await send("another self-modification is in progress — try again later")
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
