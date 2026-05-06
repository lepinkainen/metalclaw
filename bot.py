import argparse
import json
import re
import shlex
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import httpx
from prompt_toolkit import PromptSession
from rich.console import Console
from rich.markdown import Markdown

import memory
import self_change
from config import get_config
from history import SQLiteHistory
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
    "remember": "save a preference: /remember <key>=<value>",
    "forget": "remove a memory entry: /forget <matcher>",
    "memory": "show your stored long-term memory",
    "onboard": "answer a few questions to seed long-term memory",
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
    "user; call get_user_memory to read the full file."
)


def build_system_prompt(scope: str, now: str) -> str:
    base = _SYSTEM_PROMPT_BASE.format(now=now)
    summary = memory.summary(scope)
    if summary:
        base += f"\n\nKnown about user (scope={scope}):\n{summary}"
    return base

_CLIENT = httpx.Client(timeout=120.0)


# --- Ollama client ---


def _tool_result_json(result: object) -> str:
    return json.dumps(result, ensure_ascii=False)


def chat(
    messages: list[dict],
    on_tool_call: Callable[[str, dict, str], None] | None = None,
) -> str:
    """Send messages to Ollama, handle tool calls in a loop, return final text.

    Note: mutates `messages` in place, appending tool-call/result entries
    so the caller retains full conversation history.
    """
    tool_schemas = [t.schema for t in TOOLS.values()]

    cfg = get_config()
    while True:
        response = _CLIENT.post(
            cfg.ollama_url,
            json={
                "model": cfg.model,
                "messages": messages,
                "tools": tool_schemas or None,
                "stream": False,
            },
        )
        response.raise_for_status()
        msg = response.json()["message"]

        if not msg.get("tool_calls"):
            return msg.get("content", "")

        # Append assistant message (with tool_calls) to history
        messages.append(msg)

        # Execute each tool call and append results
        for tc in msg["tool_calls"]:
            name = tc["function"]["name"]
            args = tc["function"]["arguments"]
            tool_obj = TOOLS.get(name)
            if tool_obj is None:
                result = f"Error: unknown tool '{name}'"
            else:
                try:
                    result = tool_obj.func(**args)
                except Exception as e:
                    result = f"Error: {e}"

            result_json = _tool_result_json(result)
            short_result = result_json[:120]
            if on_tool_call:
                on_tool_call(name, args, short_result)

            messages.append({"role": "tool", "content": result_json, "name": name})


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


def _handle_onboard(_: str) -> None:
    mem = memory.load()
    if mem.preferences:
        console.print(
            "[dim]already onboarded — use /memory to inspect, /forget to remove entries[/dim]"
        )
        return
    if _prompt_session is None:
        console.print("onboarding requires the interactive prompt")
        return
    console.print("[dim]onboarding — answer briefly, blank to skip[/dim]")
    for key, question in _ONBOARDING_STEPS:
        try:
            answer = _prompt_session.prompt(f"{question}\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]onboarding aborted[/dim]")
            return
        if not answer:
            continue
        value = _format_interests(answer) if key == "interests" else answer
        memory.set_preference(key, value)
    console.print("[dim]done — restart for memory to enter the system prompt[/dim]")


_COMMAND_HANDLERS = {
    "add-tool":  _handle_add_tool,
    "self-edit": _handle_self_edit,
    "think":     _handle_think,
    "train":     _make_tool_handler("train_departures", _parse_train_args,   _format_train_result),
    "weather":   _make_tool_handler("weather",          _parse_weather_args, _format_weather_result),
    "mail":      _make_tool_handler("list_emails",      _parse_mail_args,    _format_mail_result),
    "remember":  _handle_remember,
    "forget":    _handle_forget,
    "memory":    _handle_memory,
    "onboard":   _handle_onboard,
    "help":      _handle_help,
}


def _cli_tool_log(name: str, args: dict, short_result: str) -> None:
    if not _show_thinking:
        return
    args_summary = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
    console.print(f"  [dim][tool: {name}({args_summary})] → {short_result}[/dim]")


def main():
    import tools  # noqa: F401 — triggers @tool registrations

    global _prompt_session
    memory.current_scope.set("cli")

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

    console.print(f"metalclaw bot ({get_config().model}) — type 'quit' to exit")
    console.print(f"  {len(TOOLS)} tool(s) loaded: {', '.join(TOOLS)}\n")

    while True:
        try:
            user_input = _prompt_session.prompt("you> ").strip()
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

        messages.append({"role": "user", "content": user_input})

        try:
            with console.status("[dim]thinking…[/dim]", spinner="dots"):
                reply = chat(messages, on_tool_call=_cli_tool_log)
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


if __name__ == "__main__":
    main()
