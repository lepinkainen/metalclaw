"""CLI REPL frontend: prompt_toolkit input loop, slash-command handlers, rich rendering."""

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import run_in_terminal
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console
from rich.markdown import Markdown

import channels
import memory
import self_change
from chat_loop import (
    _parse_command,
    build_system_prompt,
    chat,
    chat_via_escalation,
    run_turn,
)
from config import get_config
from frontends import common
from history import SQLiteHistory
from registry import TOOLS

REPO_ROOT = Path(__file__).resolve().parent.parent

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
    "heartbeat": "show heartbeat config / run a tick now (/heartbeat run)",
    "big": "ask the escalation cloud model directly: /big <query>",
    "help": "show this help",
}

_show_thinking = False
_prompt_session: PromptSession | None = None
_cli_messages: list[dict] | None = None


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


def _cli_messages_ref() -> list[dict] | None:
    return _cli_messages


async def _handle_big(args: str) -> None:
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
    try:
        with console.status("[dim]asking the big model…[/dim]", spinner="dots"):
            _, _, clean_reply = await run_turn(
                messages,
                query,
                lambda: chat_via_escalation(messages, on_tool_call=_cli_tool_log),
            )
    except Exception as e:
        console.print(f"\n[bold]bot>[/bold] Error: {e}\n")
        return
    _print_bot_markdown(clean_reply)


async def _cli_send_dim(msg: str) -> None:
    console.print(f"[dim]{msg}[/dim]")


async def _handle_remember(args: str) -> None:
    await common.run_remember(_cli_send_dim, args)


async def _handle_forget(args: str) -> None:
    await common.run_forget(_cli_send_dim, args)


def _handle_memory(_: str) -> None:
    _print_bot_markdown(memory.render_full())


async def _handle_heartbeat(args: str) -> None:
    await common.run_heartbeat(_cli_send_dim, "cli", args.strip())


_COMMAND_HANDLERS = {
    "add-tool":  _handle_add_tool,
    "self-edit": _handle_self_edit,
    "think":     _handle_think,
    "train":     _make_tool_handler(*common.TOOL_COMMANDS["train"]),
    "weather":   _make_tool_handler(*common.TOOL_COMMANDS["weather"]),
    "mail":      _make_tool_handler(*common.TOOL_COMMANDS["mail"]),
    "search":    _make_tool_handler(*common.TOOL_COMMANDS["search"]),
    "remember":  _handle_remember,
    "forget":    _handle_forget,
    "memory":    _handle_memory,
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


async def run_cli_repl() -> None:
    global _prompt_session, _cli_messages
    channels.register(_CLIChannel())

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    hist = SQLiteHistory(session_id)
    _prompt_session = PromptSession(history=hist)

    messages: list[dict] = [
        {
            "role": "system",
            "content": build_system_prompt(now),
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
                    result = handler(args)
                    if asyncio.iscoroutine(result):
                        await result
                except ValueError as e:
                    console.print(str(e))
                continue

            try:
                with console.status("[dim]thinking…[/dim]", spinner="dots"):
                    reply, thinking, clean_reply = await run_turn(
                        messages,
                        user_input,
                        lambda: chat(messages, on_tool_call=_cli_tool_log),
                    )
            except Exception as e:
                console.print(f"\n[bold]bot>[/bold] Error: {e}\n")
                continue

            hist.save_assistant(reply)

            if thinking and _show_thinking:
                console.print(f"\n[dim]{thinking}[/dim]")
            _print_bot_markdown(clean_reply)
