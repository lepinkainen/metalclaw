import uuid
from typing import Any

import httpx

from providers.base import AssistantMessage, ToolCall


_CLIENT = httpx.Client(timeout=120.0)


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        url: str,
        model: str,
        temperature: float | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.top_k = top_k

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage:
        payload_messages: list[dict[str, Any]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(messages)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": payload_messages,
            "tools": tools or None,
            "stream": False,
        }
        options: dict[str, Any] = {}
        if self.temperature is not None:
            options["temperature"] = self.temperature
        if self.top_p is not None:
            options["top_p"] = self.top_p
        if self.top_k is not None:
            options["top_k"] = self.top_k
        if options:
            payload["options"] = options

        response = _CLIENT.post(self.url, json=payload)
        response.raise_for_status()
        msg = response.json()["message"]

        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                # Defensive: some Ollama builds emit JSON-string arguments.
                import json
                args = json.loads(args) if args.strip() else {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"ollama-{uuid.uuid4().hex[:12]}",
                    name=fn["name"],
                    arguments=args,
                )
            )

        return AssistantMessage(
            text=msg.get("content", "") or "",
            tool_calls=tool_calls,
            raw=msg,
        )

    def format_tool_results(
        self,
        results: list[tuple[ToolCall, str]],
    ) -> list[dict]:
        return [
            {"role": "tool", "name": call.name, "content": result_json}
            for call, result_json in results
        ]


def fetch_model_defaults(chat_url: str, model: str) -> dict[str, Any]:
    """Query Ollama /api/show and parse PARAMETER lines from the modelfile.

    Returns a dict possibly containing keys "temperature", "top_p", "top_k".
    On any error, returns {}.
    """
    show_url = chat_url.replace("/api/chat", "/api/show")
    try:
        r = _CLIENT.post(show_url, json={"name": model}, timeout=5.0)
        r.raise_for_status()
        params_text = r.json().get("parameters", "") or ""
    except Exception:
        return {}
    out: dict[str, Any] = {}
    for line in params_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        key, val = parts[0], parts[1]
        if key in ("temperature", "top_p"):
            try:
                out[key] = float(val)
            except ValueError:
                pass
        elif key == "top_k":
            try:
                out[key] = int(val)
            except ValueError:
                pass
    return out
