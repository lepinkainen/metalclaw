import uuid
from typing import Any

import httpx

from providers.base import AssistantMessage, ToolCall


_CLIENT = httpx.Client(timeout=120.0)


class OllamaProvider:
    name = "ollama"

    def __init__(self, url: str, model: str) -> None:
        self.url = url
        self.model = model

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

        response = _CLIENT.post(
            self.url,
            json={
                "model": self.model,
                "messages": payload_messages,
                "tools": tools or None,
                "stream": False,
            },
        )
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
