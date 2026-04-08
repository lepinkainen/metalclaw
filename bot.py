import re
from datetime import datetime
from pathlib import Path

import httpx
from prompt_toolkit import PromptSession
from rich.console import Console

import self_change
from history import SQLiteHistory
from registry import TOOLS

REPO_ROOT = Path(__file__).parent.resolve()

console = Console(highlight=False)

_COMMANDS = {
    "add-tool": "add a new tool — describe what it should do",
    "self-edit": "make a general code change — describe what to change",
    "think": "toggle display of model thinking (off by default)",
    "help": "show this help",
}

_show_thinking = False

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


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "gemma4:latest"

_CLIENT = httpx.Client(timeout=120.0)


# --- Ollama client ---


def chat(messages: list[dict]) -> str:
    """Send messages to Ollama, handle tool calls in a loop, return final text.

    Note: mutates `messages` in place, appending tool-call/result entries
    so the caller retains full conversation history.
    """
    tool_schemas = [t.schema for t in TOOLS.values()]

    while True:
        response = _CLIENT.post(
            OLLAMA_URL,
            json={
                "model": MODEL,
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

            args_summary = ", ".join(f"{k}={v!r}" for k, v in args.items()) if args else ""
            short_result = str(result)[:120]
            console.print(f"  [dim][tool: {name}({args_summary})] → {short_result}[/dim]")

            messages.append({"role": "tool", "content": str(result), "name": name})


# --- CLI ---


def main():
    global _show_thinking

    import tools  # noqa: F401 — triggers @tool registrations

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    session_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    hist = SQLiteHistory(session_id)
    prompt_session = PromptSession(history=hist)

    messages: list[dict] = [
        {
            "role": "system",
            "content": f"You are Metalclaw, a helpful assistant. The current date and time is {now}. When a user request can be fulfilled by calling a tool, you MUST use the tool rather than simulating or writing code. Never generate fake results.",
        },
    ]

    console.print(f"metalclaw bot ({MODEL}) — type 'quit' to exit")
    console.print(f"  {len(TOOLS)} tool(s) loaded: {', '.join(TOOLS)}\n")

    while True:
        try:
            user_input = prompt_session.prompt("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input or user_input.lower() in ("quit", "exit"):
            break

        parsed = _parse_command(user_input)
        if parsed is not None:
            cmd, args = parsed
            if cmd in ("add-tool", "self-edit"):
                request = f"Add a new tool: {args}" if cmd == "add-tool" else args
                result = self_change.run_self_change(request, REPO_ROOT)
                status = "approved" if result.approved else "rejected"
                console.print(f"\n[dim][self-change {status}][/dim]\n")
            elif cmd == "think":
                _show_thinking = not _show_thinking
                state = "on" if _show_thinking else "off"
                console.print(f"[dim]thinking display {state}[/dim]")
            elif cmd == "help":
                for name, desc in _COMMANDS.items():
                    console.print(f"  /{name}  —  {desc}")
            else:
                console.print(f"unknown command: /{cmd}  (try /help)")
            continue

        messages.append({"role": "user", "content": user_input})

        with console.status("[dim]thinking…[/dim]", spinner="dots"):
            reply = chat(messages)

        messages.append({"role": "assistant", "content": reply})
        hist.save_assistant(reply)

        thinking, clean_reply = _split_thinking(reply)
        if thinking and _show_thinking:
            console.print(f"\n[dim]{thinking}[/dim]")
        console.print(f"\n[bold]bot>[/bold] {clean_reply}\n")


if __name__ == "__main__":
    main()
