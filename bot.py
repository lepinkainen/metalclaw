import httpx

from registry import TOOLS

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

    messages: list[dict] = [
        {"role": "system", "content": "You are a helpful assistant. When a user request can be fulfilled by calling a tool, you MUST use the tool rather than simulating or writing code. Never generate fake results."},
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

        messages.append({"role": "user", "content": user_input})
        reply = chat(messages)
        messages.append({"role": "assistant", "content": reply})
        print(f"\nbot> {reply}\n")


if __name__ == "__main__":
    main()
