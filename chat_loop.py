"""Provider-agnostic chat loop, system-prompt builder, and slash-command parser.

No frontend dependencies. Imported by every frontend (CLI, Telegram, Discord)
and by tests directly.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from pydantic import ValidationError

import heartbeat
import history as history_db
import mcp_client
import memory
from config import get_config
from heartbeat import format_now
from providers import Provider, get_provider
from registry import TOOLS

log = logging.getLogger(__name__)

__all__ = [
    "_active_session_messages",
    "_chat_with_provider",
    "_parse_command",
    "_refresh_system_prompt",
    "_run_tool",
    "_split_system",
    "_split_thinking",
    "build_system_prompt",
    "chat",
    "chat_via_escalation",
    "current_scope",
    "forget_session_provider",
    "format_now",
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
    "The current date and time is {now}. "
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


def build_system_prompt(now: str) -> str:
    base = _SYSTEM_PROMPT_BASE.format(now=now)
    cfg = get_config()
    if cfg.escalation_enabled and cfg.provider == "ollama":
        base += "\n\n" + _ESCALATION_HINT
    mcp_note = mcp_client.system_prompt_note()
    if mcp_note:
        base += "\n\n" + mcp_note
    mem_summary = memory.summary()
    if mem_summary:
        base += f"\n\nKnown about user:\n{mem_summary}"
    hb_summary = heartbeat.summary()
    if hb_summary:
        base += f"\n\nActive heartbeat actions:\n{hb_summary}"
    return base


def _refresh_system_prompt(messages: list[dict]) -> None:
    """Rewrite messages[0] with current memory summary so mid-session writes are visible."""
    now = format_now()
    prompt = build_system_prompt(now)
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": prompt}
    else:
        messages.insert(0, {"role": "system", "content": prompt})
    log.debug(
        "system prompt refreshed (%d chars, %d messages)", len(prompt), len(messages)
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


# Snapshot of the current session's messages list, set on each chat() entry so
# tools (notably escalate_to_big_model) can read full conversation context.
_active_session_messages: ContextVar[list[dict] | None] = ContextVar(
    "active_session_messages", default=None
)


# Channel scope of the current chat turn — set by each frontend before chat()
# runs (CLI: "cli"; Telegram: "telegram-<chat_id>"; Discord: "discord-<id>").
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


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    if messages and messages[0].get("role") == "system":
        return messages[0].get("content", "") or "", list(messages[1:])
    return "", list(messages)


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
    messages: list[dict],
    *,
    on_tool_call: Callable[[str, dict, str], None] | None = None,
    exclude_tools: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Run the tool-call loop against `provider`, mutating `messages` in place."""
    tool_schemas = [t.schema for name, t in TOOLS.items() if name not in exclude_tools]
    system, history = _split_system(messages)
    token = _active_session_messages.set(messages)
    pname = getattr(provider, "name", type(provider).__name__)
    pmodel = getattr(provider, "model", None)
    turn_t0 = time.monotonic()
    log.info(
        "turn start provider=%s model=%s scope=%s history=%d messages tools=%d system=%d chars",
        pname,
        pmodel,
        current_scope.get(),
        len(history),
        len(tool_schemas),
        len(system),
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
            call_t0 = time.monotonic()
            try:
                am = provider.chat_once(history, schemas, system)
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
                # Model ignored the empty tool list. Do not record its
                # tool_calls (no results would follow -> broken history);
                # surface what we have.
                log.error(
                    "tool loop: model still emitted %d tool call(s) after cap; giving up",
                    len(am.tool_calls),
                )
                messages[:] = (
                    [{"role": "system", "content": system}] if system else []
                ) + history
                return am.text or (
                    f"Error: stopped after {max_iterations} tool-call rounds "
                    "without a final answer."
                )

            raw = am.raw
            if isinstance(raw, list):
                history.extend(raw)
            elif raw is not None:
                history.append(raw)

            if not am.tool_calls:
                log.info(
                    "turn end provider=%s iterations=%d duration=%.2fs reply=%d chars",
                    pname,
                    iteration,
                    time.monotonic() - turn_t0,
                    len(am.text),
                )
                log.debug("turn reply: %.500s", am.text)
                messages[:] = (
                    [{"role": "system", "content": system}] if system else []
                ) + history
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

            results: list[tuple] = []
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
                results.append((tc, result_json))
                if tc.name in _MEMORY_MUTATORS:
                    memory_dirty = True
            history.extend(provider.format_tool_results(results))

            if memory_dirty:
                now = format_now()
                system = build_system_prompt(now)
                log.info(
                    "system prompt rebuilt mid-loop after memory/heartbeat mutator (%d chars)",
                    len(system),
                )
    finally:
        _active_session_messages.reset(token)


# Session -> provider name. Each call to `chat()` stamps the session so we can
# detect a mid-session config switch (Ollama and litellm both produce
# OpenAI-shaped envelopes, but tool-call ids and message-result formats still
# differ enough that history must be reset on a swap).
_session_providers: dict[int, str] = {}


def forget_session_provider(messages: list[dict]) -> None:
    """Drop the provider stamp for `messages` (call on /new or session disposal)."""
    _session_providers.pop(id(messages), None)


def _check_session_provider(messages: list[dict], provider_name: str) -> bool:
    """Stamp the session; if the provider switched, drop tainted history.

    Keeps the leading system message and the trailing user turn (both wire-
    format-agnostic) and discards everything in between. Returns True if a
    reset happened.
    """
    sid = id(messages)
    stamped = _session_providers.get(sid)
    _session_providers[sid] = provider_name
    if stamped is None or stamped == provider_name:
        return False
    kept: list[dict] = []
    if messages and messages[0].get("role") == "system":
        kept.append(messages[0])
    if (
        messages
        and messages[-1].get("role") == "user"
        and (not kept or messages[-1] is not kept[0])
    ):
        kept.append(messages[-1])
    log.warning(
        "provider switch %s -> %s: dropped history %d -> %d messages",
        stamped,
        provider_name,
        len(messages),
        len(kept),
    )
    messages[:] = kept
    return True


def chat(
    messages: list[dict],
    on_tool_call: Callable[[str, dict, str], None] | None = None,
) -> str:
    """Send messages via the configured provider, handle tool calls, return final text.

    Mutates `messages` in place, appending tool-call/result entries so the
    caller retains full conversation history. If the configured provider
    changed since this session's last turn, prior tool-call history is
    dropped (system + current user turn preserved) so the new provider does
    not choke on foreign wire formats.
    """
    cfg = get_config()
    _check_session_provider(messages, cfg.provider)
    provider = get_provider(cfg.provider)
    return _chat_with_provider(provider, messages, on_tool_call=on_tool_call)


def strip_tool_history(messages: list[dict]) -> list[dict]:
    """Return a copy of `messages` with only wire-format-agnostic turns.

    Tool-call history is provider-specific (Ollama tool messages carry no
    ``tool_call_id``; litellm assistant tool_calls carry ids and JSON-string
    arguments). When a session built on one provider is handed to another,
    drop ``role: tool`` messages entirely and keep assistant turns as plain
    text only, so the receiving provider never sees a foreign payload.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "tool":
            continue
        if role == "assistant":
            text = m.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            out.append({"role": "assistant", "content": text})
            continue
        out.append(m)
    return out


def chat_via_escalation(
    messages: list[dict],
    on_tool_call: Callable[[str, dict, str], None] | None = None,
) -> str:
    """Run a chat turn through the escalation provider, no recursion into itself.

    When the escalation provider differs from the session's provider the turn
    runs on a sanitised copy of the history (see `strip_tool_history`) and only
    the final assistant text is appended back to `messages`, so neither
    provider's tool-call wire format ever leaks into the other's session.
    """
    cfg = get_config()
    if not cfg.escalation_enabled:
        raise RuntimeError("escalation is disabled in config")
    big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    cross_provider = cfg.escalation_provider != cfg.provider
    work = strip_tool_history(messages) if cross_provider else messages
    log.info(
        "escalation start provider=%s model=%s messages=%d",
        cfg.escalation_provider,
        cfg.escalation_model,
        len(messages),
    )
    t0 = time.monotonic()
    try:
        out = _chat_with_provider(
            big,
            work,
            on_tool_call=on_tool_call,
            exclude_tools={"escalate_to_big_model"},
        )
    except Exception:
        log.exception("escalation failed after %.2fs", time.monotonic() - t0)
        raise
    if cross_provider:
        messages.append({"role": "assistant", "content": out})
    log.info(
        "escalation end duration=%.2fs reply=%d chars cross_provider=%s",
        time.monotonic() - t0,
        len(out),
        cross_provider,
    )
    return out


async def run_turn(
    messages: list[dict],
    user_text: str,
    chat_call: Callable[[], str],
) -> tuple[str, str, str]:
    """Single user-turn orchestration shared by every frontend.

    Refreshes the system prompt, appends ``user_text``, runs ``chat_call`` in a
    thread executor, then splits ``<think>`` tags from the result. Returns
    ``(raw_reply, thinking, clean_reply)``. On any exception from the chat call,
    pops the user message from ``messages`` (so the caller's session state
    stays consistent) and re-raises.

    ``chat_call`` is a no-arg closure so callers can bind ``chat`` /
    ``chat_via_escalation`` plus any ``on_tool_call`` hook themselves.
    """
    _refresh_system_prompt(messages)
    messages.append({"role": "user", "content": user_text})
    log.info(
        "run_turn scope=%s session=%d messages user_text=%d chars",
        current_scope.get(),
        len(messages),
        len(user_text),
    )
    log.debug("run_turn user text: %.500s", user_text)
    loop = asyncio.get_running_loop()
    try:
        reply = await loop.run_in_executor(None, chat_call)
    except Exception:
        log.exception("run_turn failed; reverting user message")
        messages.pop()
        raise
    thinking, clean_reply = _split_thinking(reply)
    return reply, thinking, clean_reply
