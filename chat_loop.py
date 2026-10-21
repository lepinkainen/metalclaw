"""Provider-agnostic chat loop, system-prompt builder, and slash-command parser.

No frontend dependencies. Imported by every frontend (Telegram, Discord)
and by tests directly.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

import heartbeat
import history as history_db
import mcp_client
import memory
from config import get_config
from heartbeat import format_now
from messages import (
    SYSTEM_CACHE_BOUNDARY,
    AssistantMessage,
    Message,
    ToolResultMessage,
    UserMessage,
    validate_exchange,
)
from providers import Provider, get_provider
from registry import TOOLS

log = logging.getLogger(__name__)

__all__ = [
    "Session",
    "_active_session",
    "_chat_with_provider",
    "_parse_command",
    "_refresh_system_prompt",
    "_run_tool",
    "_split_thinking",
    "build_system_prompt",
    "chat",
    "chat_via_escalation",
    "current_scope",
    "format_now",
    "new_session",
    "run_turn",
    "scoped_chat",
]

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _split_thinking(text: str) -> tuple[str, str]:
    """Return (thinking_content, reply_without_think_tags)."""
    thinking_parts = _THINK_RE.findall(text)
    clean = _THINK_RE.sub("", text).strip()
    if thinking_parts:
        log.debug(
            "think-strip: %d block(s), removed %d chars, reply %d -> %d chars",
            len(thinking_parts),
            len(text) - len(clean),
            len(text),
            len(clean),
        )
    return "\n".join(thinking_parts).strip(), clean


def _parse_command(text: str) -> tuple[str, str] | None:
    """Return (command, args) if text starts with /, else None."""
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    if not parts:
        # bare "/" (or "/   ") — surface as an unknown command, never IndexError
        return "", ""
    return parts[0], parts[1] if len(parts) > 1 else ""


_SYSTEM_PROMPT_BASE = (
    "You are Metalclaw, a helpful assistant. "
    "When a user request can be fulfilled by calling a tool, you MUST use the tool "
    "rather than simulating or writing code. Never generate fake results. "
    "Never claim you scheduled, set, remembered, cancelled, or performed an action "
    "unless a tool call in this turn returned success for it. If you cannot call "
    "the required tool, say so plainly instead of confirming. "
    "When a tool returns factual data, present it directly and naturally. "
    "Do not add generic warnings, hedging, or suggestions to verify elsewhere unless "
    "the tool result itself indicates uncertainty, staleness, or an error. "
    "If a tool returns source metadata, treat it as authoritative context for how to "
    "describe the result. "
    "You have long-term memory across sessions: call set_user_preference, "
    "add_user_fact, or forget_user_memory to record durable information about the "
    "user; call get_user_memory to read the full file. "
    "If a request needs more info, check memory first before asking the user. "
    "When the user gives information, store it in memory if relevant. "
    "For reminders, scheduled checks, or watch-this-for-me requests, schedule a "
    "heartbeat action with create_heartbeat_action (kind='at' for one-shots, "
    "'cron' for weekday schedules, 'every' for interval polling). Use "
    "list_heartbeat_actions to inspect what's scheduled and "
    "cancel_heartbeat_action to remove one. Do not store reminders in memory. "
    "When asked about your own features, slash commands, memory, heartbeat, "
    "escalation, self-modification, or frontends, call read_manual rather "
    "than guessing."
)


_ESCALATION_HINT = (
    "When a question is beyond your capability — complex reasoning, deep code "
    "analysis, niche knowledge — call escalate_to_big_model rather than "
    "guessing. Pass the user's question and a short reason. Do not escalate "
    "trivial requests."
)


_HISTORY_TRIMMED_NOTE = (
    "Earlier parts of this conversation were archived and are not shown. If the "
    "user refers to something you cannot see, use search_conversation_history "
    "instead of guessing."
)


def build_system_prompt(now: str, *, history_trimmed: bool = False) -> str:
    """Stable instructions, then ``SYSTEM_CACHE_BOUNDARY``, then the volatile tail.

    The clock, memory summary and heartbeat summary change between calls, so
    they live after the boundary where a caching provider leaves them out of
    the cached prefix (see ``providers.litellm_provider._system_message``).
    ``history_trimmed`` adds the archived-history note (see ``Session.load``).
    """
    cfg = get_config()
    base = _SYSTEM_PROMPT_BASE
    if cfg.escalation_enabled and cfg.provider == "ollama":
        base += "\n\n" + _ESCALATION_HINT
    mcp_note = mcp_client.system_prompt_note()
    if mcp_note:
        base += "\n\n" + mcp_note
    tail = f"The current date and time is {now}."
    if history_trimmed:
        tail += "\n\n" + _HISTORY_TRIMMED_NOTE
    mem_summary = memory.summary()
    if mem_summary:
        tail += f"\n\nKnown about user:\n{mem_summary}"
    hb_summary = heartbeat.summary()
    if hb_summary:
        tail += f"\n\nActive heartbeat actions:\n{hb_summary}"
    return base + SYSTEM_CACHE_BOUNDARY + tail


@dataclass
class Session:
    """One conversation: the system prompt plus canonical message history.

    ``history`` holds only ``messages.Message`` values (frozen), never a
    provider's wire dicts, so the active provider may change between turns
    without any reset. The system prompt is kept apart so it can be rebuilt
    without touching history.

    A session with a ``scope`` is backed by the ``turns`` table in history.db:
    ``Session.load(scope)`` rebuilds it from the rows after the latest ``/new``
    boundary and ``commit()`` appends the new messages, so conversations
    survive restarts and no frontend keeps sessions in memory. Heartbeat runs
    and tests use scope-less, in-memory sessions.

    Because nothing else ever shortens a persisted conversation, ``load``
    trims: a chat idle for ``session_idle_reset_seconds`` restarts from a new
    boundary, and at most ``session_max_messages`` recent messages are
    replayed. Either case sets ``history_trimmed`` so the system prompt tells
    the model to search history rather than guess.
    """

    system: str = ""
    history: list[Message] = field(default_factory=list)
    scope: str | None = None
    history_trimmed: bool = False
    # How many leading ``history`` entries are already in the turns table.
    _persisted: int = field(default=0, repr=False, compare=False)

    @classmethod
    def load(cls, scope: str) -> Session:
        """Session for ``scope``: persisted history (idle-expired / windowed) + fresh prompt."""
        cfg = get_config()
        trimmed = False
        last_ts = history_db.turns_last_ts(scope)
        if last_ts is not None and cfg.session_idle_reset_seconds > 0:
            idle = datetime.now(UTC) - datetime.fromisoformat(last_ts)
            if idle.total_seconds() > cfg.session_idle_reset_seconds:
                history_db.turns_reset(scope)
                log.info(
                    "session idle-reset scope=%s idle=%ds", scope, idle.total_seconds()
                )
                trimmed = True
        msgs, skipped = history_db.turns_window(scope, cfg.session_max_messages)
        if skipped:
            log.info(
                "session windowed scope=%s skipped=%d kept=%d",
                scope,
                skipped,
                len(msgs),
            )
            trimmed = True
        log.debug(
            "session load scope=%s messages=%d trimmed=%s", scope, len(msgs), trimmed
        )
        return cls(
            system=build_system_prompt(format_now(), history_trimmed=trimmed),
            history=msgs,
            scope=scope,
            history_trimmed=trimmed,
            _persisted=len(msgs),
        )

    @staticmethod
    def reset(scope: str) -> None:
        """``/new``: start an empty conversation for ``scope`` (rows stay searchable)."""
        history_db.turns_reset(scope)
        log.info("session reset scope=%s", scope)

    def commit(self, work: Session) -> None:
        """Adopt ``work`` (a finished turn on a snapshot) and persist what is new.

        Persist first: if the write fails the caller's exception path leaves
        ``self`` untouched, exactly like a provider error.
        """
        if self.scope is not None:
            history_db.turns_append(self.scope, work.history[self._persisted :])
        self.system, self.history = work.system, work.history
        self._persisted = len(self.history)

    def append_user(self, text: str) -> None:
        self.history.append(UserMessage(text))

    def append_exchange(
        self, assistant: AssistantMessage, results: Sequence[ToolResultMessage]
    ) -> None:
        """Commit one tool round, refusing unmatched / duplicate result ids."""
        validate_exchange(assistant, results)
        self.history.append(assistant)
        self.history.extend(results)

    def snapshot(self) -> Session:
        """Point-in-time copy for tools; entries are frozen so they are shared."""
        return Session(system=self.system, history=list(self.history))


def new_session() -> Session:
    """Session with a fresh system prompt (memory + heartbeat summary) and no history."""
    return Session(system=build_system_prompt(format_now()))


def _refresh_system_prompt(session: Session) -> None:
    """Rebuild ``session.system`` so mid-session memory writes are visible."""
    session.system = build_system_prompt(
        format_now(), history_trimmed=session.history_trimmed
    )
    log.debug(
        "system prompt refreshed (%d chars, %d messages)",
        len(session.system),
        len(session.history),
    )


_MEMORY_MUTATORS = frozenset(
    {
        "set_user_preference",
        "add_user_fact",
        "add_user_instruction",
        "forget_user_memory",
        "create_heartbeat_action",
        "cancel_heartbeat_action",
    }
)


def _tool_result_json(result: object) -> str:
    return json.dumps(result, ensure_ascii=False)


# Snapshot of the running turn's conversation (system + completed exchanges),
# republished by `_chat_with_provider` before every provider call so tools
# (notably escalate_to_big_model) see this turn's earlier tool results. The
# assistant message currently being resolved is never included: its tool
# calls have no results yet and would leave a dangling call in the snapshot.
_active_session: ContextVar[Session | None] = ContextVar("active_session", default=None)


# Channel scope of the current chat turn — set by each frontend before chat()
# runs (Telegram: "telegram-<chat_id>"; Discord: "discord-<channel_id>").
# Tools that need to know which user-facing surface produced the call
# (notably create_heartbeat_action) read it. None during heartbeat ticks and
# in tests.
current_scope: ContextVar[str | None] = ContextVar("current_scope", default=None)


def scoped_chat(scope: str | None, fn: Callable[[], str]) -> str:
    """Run ``fn`` (a chat-call thunk) with ``current_scope`` set to ``scope``.

    Use this at every frontend's executor boundary so synchronous tool code
    invoked inside ``chat()`` can read the active scope. ``scope`` is allowed
    to be ``None`` (e.g. background ticks) — the var is then explicitly
    cleared rather than inherited from whatever ran before.
    """
    token = current_scope.set(scope)
    try:
        return fn()
    finally:
        current_scope.reset(token)


def _run_tool(name: str, args: dict) -> object:
    tool_obj = TOOLS.get(name)
    if tool_obj is None:
        log.warning("tool call: unknown tool %r args=%.500s", name, args)
        return f"Error: unknown tool '{name}'"
    if tool_obj.args_model is not None:
        try:
            validated = tool_obj.args_model.model_validate(args)
        except ValidationError as e:
            log.warning("tool call: invalid arguments for %s: %.500s", name, e.errors())
            return {
                "error": "invalid_arguments",
                "tool": name,
                "issues": [
                    {
                        "field": ".".join(str(p) for p in err["loc"]),
                        "message": err["msg"],
                        "type": err["type"],
                    }
                    for err in e.errors()
                ],
            }
        call_args = validated.model_dump()
    else:
        call_args = args
    t0 = time.monotonic()
    try:
        result = tool_obj.func(**call_args)
    except Exception as e:
        log.exception("tool call: %s raised after %.2fs", name, time.monotonic() - t0)
        return f"Error: {e}"
    log.debug("tool call: %s ok in %.2fs", name, time.monotonic() - t0)
    return result


def _chat_with_provider(
    provider: Provider,
    session: Session,
    *,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    exclude_tools: frozenset[str] | set[str] = frozenset(),
    only_tools: frozenset[str] | set[str] | None = None,
) -> str:
    """Run the tool-call loop against `provider`.

    Works on a copy of ``session`` and commits it (plus any mid-loop
    system-prompt rebuild) back to ``session`` via ``Session.commit`` only on
    a normal return, so a provider exception leaves the session (and the
    turns table) exactly as it was. ``only_tools`` narrows the schema list to
    exactly those names (watcher pipelines use it); ``exclude_tools`` then
    still applies.
    """
    tool_schemas = [
        t.schema
        for name, t in TOOLS.items()
        if name not in exclude_tools and (only_tools is None or name in only_tools)
    ]
    work = session.snapshot()
    token = _active_session.set(work.snapshot())
    pname = getattr(provider, "name", type(provider).__name__)
    pmodel = getattr(provider, "model", None)
    turn_t0 = time.monotonic()
    log.info(
        "turn start provider=%s model=%s scope=%s history=%d messages tools=%d system=%d chars",
        pname,
        pmodel,
        current_scope.get(),
        len(work.history),
        len(tool_schemas),
        len(work.system),
    )
    cfg = get_config()
    max_iterations = cfg.max_tool_iterations
    max_repeats = cfg.max_repeated_tool_calls
    iteration = 0
    last_signature: tuple[tuple[str, str], ...] | None = None
    repeat_count = 0
    try:
        while True:
            iteration += 1
            # Past the cap: one last call with no tools so the model must
            # answer in text instead of issuing yet another tool call.
            capped = iteration > max_iterations
            schemas = [] if capped else tool_schemas
            if capped:
                log.warning(
                    "tool loop hit max_tool_iterations=%d provider=%s; "
                    "forcing a tool-less final call",
                    max_iterations,
                    pname,
                )
            # Republish so tools run in this round see completed exchanges.
            _active_session.set(work.snapshot())
            call_t0 = time.monotonic()
            try:
                am = provider.chat_once(work.history, schemas, work.system)
            except Exception:
                log.exception(
                    "provider call failed provider=%s model=%s iteration=%d",
                    pname,
                    pmodel,
                    iteration,
                )
                raise
            log.info(
                "provider call #%d provider=%s latency=%.2fs text=%d chars tool_calls=%d",
                iteration,
                pname,
                time.monotonic() - call_t0,
                len(am.text),
                len(am.tool_calls),
            )
            if am.usage is not None:
                try:
                    history_db.record_usage(
                        pname,
                        str(pmodel),
                        am.usage.prompt_tokens,
                        am.usage.completion_tokens,
                        am.usage.cost_usd,
                    )
                except Exception:
                    log.exception("usage: could not record")
            if capped and am.tool_calls:
                # Model ignored the empty tool list. Its tool calls get no
                # results, so they must not enter history; keep any text it
                # produced as a plain assistant turn and surface what we have.
                log.error(
                    "tool loop: model still emitted %d tool call(s) after cap; giving up",
                    len(am.tool_calls),
                )
                if am.text:
                    work.history.append(AssistantMessage(text=am.text, usage=am.usage))
                session.commit(work)
                return am.text or (
                    f"Error: stopped after {max_iterations} tool-call rounds "
                    "without a final answer."
                )

            if not am.tool_calls:
                work.history.append(am)
                log.info(
                    "turn end provider=%s iterations=%d duration=%.2fs reply=%d chars",
                    pname,
                    iteration,
                    time.monotonic() - turn_t0,
                    len(am.text),
                )
                log.debug("turn reply: %.500s", am.text)
                session.commit(work)
                return am.text

            signature = tuple(
                (tc.name, json.dumps(tc.arguments, sort_keys=True, default=str))
                for tc in am.tool_calls
            )
            repeat_count = repeat_count + 1 if signature == last_signature else 1
            last_signature = signature
            stuck = repeat_count >= max_repeats
            if stuck:
                log.warning(
                    "tool loop: identical tool call(s) %s repeated %d times; "
                    "refusing to re-run",
                    [tc.name for tc in am.tool_calls],
                    repeat_count,
                )

            results: list[ToolResultMessage] = []
            memory_dirty = False
            for tc in am.tool_calls:
                log.debug("tool call: %s args=%.500s", tc.name, tc.arguments)
                tool_t0 = time.monotonic()
                if stuck:
                    result: Any = (
                        f"Error: you have already called {tc.name} with these exact "
                        f"arguments {repeat_count} times in a row and have its result "
                        "above. Do not call it again; answer the user now with the "
                        "information you have."
                    )
                else:
                    result = _run_tool(tc.name, tc.arguments)
                result_json = _tool_result_json(result)
                failed = (
                    isinstance(result, str)
                    and result.startswith("Error:")
                    or (isinstance(result, dict) and "error" in result)
                )
                log.info(
                    "tool %s %s in %.2fs result=%d chars",
                    tc.name,
                    "FAILED" if failed else "ok",
                    time.monotonic() - tool_t0,
                    len(result_json),
                )
                log.debug("tool %s result: %.500s", tc.name, result_json)
                if on_tool_call:
                    on_tool_call(tc.name, tc.arguments, result_json)
                results.append(
                    ToolResultMessage(call_id=tc.id, name=tc.name, content=result_json)
                )
                if tc.name in _MEMORY_MUTATORS:
                    memory_dirty = True
            work.append_exchange(am, results)

            if memory_dirty:
                now = format_now()
                work.system = build_system_prompt(now)
                log.info(
                    "system prompt rebuilt mid-loop after memory/heartbeat mutator (%d chars)",
                    len(work.system),
                )
    finally:
        _active_session.reset(token)


def chat(
    session: Session,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    *,
    only_tools: frozenset[str] | set[str] | None = None,
) -> str:
    """Run one turn via the configured provider, handle tool calls, return final text.

    History is canonical, so a ``provider:`` change between turns needs no
    reset: the new provider just serialises the same messages its own way.
    """
    cfg = get_config()
    provider = get_provider(cfg.provider)
    return _chat_with_provider(
        provider, session, on_tool_call=on_tool_call, only_tools=only_tools
    )


def chat_via_escalation(
    session: Session,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
) -> str:
    """Run a chat turn through the escalation provider, no recursion into itself.

    The escalation provider sees the full canonical history, tool rounds
    included, and its own turn is committed to ``session`` like any other.
    """
    cfg = get_config()
    if not cfg.escalation_enabled:
        raise RuntimeError("escalation is disabled in config")
    big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    log.info(
        "escalation start provider=%s model=%s messages=%d",
        cfg.escalation_provider,
        cfg.escalation_model,
        len(session.history),
    )
    t0 = time.monotonic()
    try:
        out = _chat_with_provider(
            big,
            session,
            on_tool_call=on_tool_call,
            exclude_tools={"escalate_to_big_model"},
        )
    except Exception:
        log.exception("escalation failed after %.2fs", time.monotonic() - t0)
        raise
    log.info(
        "escalation end duration=%.2fs reply=%d chars",
        time.monotonic() - t0,
        len(out),
    )
    return out


async def run_turn(
    session: Session,
    user_text: str,
    chat_call: Callable[[], str],
) -> tuple[str, str, str]:
    """Single user-turn orchestration shared by every frontend.

    Appends ``user_text``, then refreshes the system prompt and runs
    ``chat_call`` in a thread executor, then splits ``<think>`` tags from the
    result. Returns
    ``(raw_reply, thinking, clean_reply)``. On any exception from the chat call,
    pops the user message from ``session`` (so the caller's session state
    stays consistent) and re-raises.

    ``chat_call`` is a no-arg closure so callers can bind ``chat`` /
    ``chat_via_escalation`` plus any ``on_tool_call`` hook themselves.
    """
    session.append_user(user_text)
    log.info(
        "run_turn scope=%s session=%d messages user_text=%d chars",
        current_scope.get(),
        len(session.history),
        len(user_text),
    )
    log.debug("run_turn user text: %.500s", user_text)

    def _work() -> str:
        # The refresh reads memory.md and the heartbeat ledger under flock;
        # keep that off the event loop thread together with the chat call.
        _refresh_system_prompt(session)
        return chat_call()

    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, _work)
    except Exception:
        log.exception("run_turn failed; reverting user message")
        session.history.pop()
        raise
    thinking, clean_reply = _split_thinking(reply)
    return reply, thinking, clean_reply
