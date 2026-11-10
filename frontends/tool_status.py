"""Live tool-call status messages and message splitting (Telegram + Discord)."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)


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
_FENCE_HEADROOM = (
    32  # worst-case reopen prefix + close suffix added by the fence fix-up
)


def split_message(text: str, limit: int) -> list[str]:
    """Split text into ≤limit-char chunks on paragraph/line/word boundaries.

    If a fenced code block is split across chunks, close it with ``` at the end
    of one chunk and reopen with the same language fence at the start of the next.
    Shared by the Discord (2000) and Telegram (4096) frontends.
    """
    if len(text) <= limit:
        return [text]

    # Leave room for the "```lang\n" prefix / "\n```" suffix added below when
    # a fenced block spans a cut, so the fix-up never has to truncate.
    budget = max(1, limit - _FENCE_HEADROOM)
    raw_chunks: list[str] = []
    remaining = text
    while len(remaining) > budget:
        cut = remaining.rfind("\n\n", 0, budget)
        if cut <= 0:
            cut = remaining.rfind("\n", 0, budget)
        if cut <= 0:
            cut = remaining.rfind(" ", 0, budget)
        if cut <= 0:
            cut = budget
        raw_chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        raw_chunks.append(remaining)

    fixed: list[str] = []
    open_fence: str | None = None
    for chunk in raw_chunks:
        body = (f"```{open_fence}\n" if open_fence is not None else "") + chunk
        fences = re.findall(r"^```(\w*)\s*$", body, flags=re.MULTILINE)
        if len(fences) % 2 == 1:
            new_fence = fences[-1]
            body = body + "\n```"
            open_fence = new_fence
        else:
            open_fence = None
        if len(body) > limit:
            body = body[:limit]
        fixed.append(body)
    return fixed


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
