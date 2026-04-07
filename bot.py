from datetime import datetime
from pathlib import Path

import httpx

import self_change
from registry import TOOLS

REPO_ROOT = Path(__file__).parent.resolve()

_COMMANDS = {
    "add-tool": "add a new tool — describe what it should do",
    "self-edit": "make a general code change — describe what to change",
    "help": "show this help",
}


def _parse_command(text: str) -> tuple[str, str] | None:
    """Return (command, args) if text starts with /, else None."""
    if not text.startswith("/"):
        return None
    parts = text[1:].split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


OLLAMA_URL = "http://localhost:11434/api/chat"
MODEL = "qwen3.5:9b"

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
            messages.append({"role": "tool", "content": str(result), "name": name})


# --- CLI ---

def main():
    import tools  # noqa: F401 — triggers @tool registrations

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    messages: list[dict] = [
        {"role": "system", "content": f"You are Metalclaw, a helpful assistant. The current date and time is {now}. When a user request can be fulfilled by calling a tool, you MUST use the tool rather than simulating or writing code. Never generate fake results."},
    ]

    print(f"metalclaw bot ({MODEL}) — type 'quit' to exit")
    print(f"  {len(TOOLS)} tool(s) loaded: {', '.join(TOOLS)}\n")

    while True:
        try:
            user_input = input("you> ").strip()
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
                print(f"\n[self-change {status}]\n")
            elif cmd == "help":
                for name, desc in _COMMANDS.items():
                    print(f"  /{name}  —  {desc}")
            else:
                print(f"unknown command: /{cmd}  (try /help)")
            continue

        messages.append({"role": "user", "content": user_input})
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        print(f"\nbot> {reply}\n")


if __name__ == "__main__":
    main()
