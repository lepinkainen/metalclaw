"""Provider-agnostic chat loop, system-prompt builder, and slash-command parser.

No frontend dependencies. Imported by every frontend (CLI, Telegram, Discord)
and by tests directly.
"""

import json
import re
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime

import memory
from config import get_config
from providers import Provider, get_provider
from registry import TOOLS

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


def build_system_prompt(now: str) -> str:
    base = _SYSTEM_PROMPT_BASE.format(now=now)
    cfg = get_config()
    if cfg.escalation_enabled and cfg.provider == "ollama":
        base += "\n\n" + _ESCALATION_HINT
    summary = memory.summary()
    if summary:
        base += f"\n\nKnown about user:\n{summary}"
    return base


def _refresh_system_prompt(messages: list[dict]) -> None:
    """Rewrite messages[0] with current memory summary so mid-session writes are visible."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prompt = build_system_prompt(now)
    if messages and messages[0].get("role") == "system":
        messages[0] = {"role": "system", "content": prompt}
    else:
        messages.insert(0, {"role": "system", "content": prompt})


_MEMORY_MUTATORS = frozenset(
    {
        "set_user_preference",
        "add_user_fact",
        "add_user_instruction",
        "forget_user_memory",
    }
)


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
            memory_dirty = False
            for tc in am.tool_calls:
                result = _run_tool(tc.name, tc.arguments)
                result_json = _tool_result_json(result)
                if on_tool_call:
                    on_tool_call(tc.name, tc.arguments, result_json[:120])
                results.append((tc, result_json))
                if tc.name in _MEMORY_MUTATORS:
                    memory_dirty = True
            history.extend(provider.format_tool_results(results))

            if memory_dirty:
                now = datetime.now().strftime("%Y-%m-%d %H:%M")
                system = build_system_prompt(now)
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
