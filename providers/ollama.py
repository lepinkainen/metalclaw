import json
import logging
import time
import uuid
from collections.abc import Sequence
from typing import Any

import httpx

from messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

log = logging.getLogger(__name__)

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

    @staticmethod
    def _to_wire(history: Sequence[Message]) -> list[dict[str, Any]]:
        """Canonical messages -> Ollama /api/chat message dicts."""
        out: list[dict[str, Any]] = []
        for m in history:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.content})
            elif isinstance(m, AssistantMessage):
                entry: dict[str, Any] = {"role": "assistant", "content": m.text}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {"function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in m.tool_calls
                    ]
                out.append(entry)
            elif isinstance(m, ToolResultMessage):
                out.append({"role": "tool", "name": m.name, "content": m.content})
            else:  # pragma: no cover - exhaustiveness guard
                raise TypeError(f"unsupported message type: {type(m).__name__}")
        return out

    def chat_once(
        self,
        history: Sequence[Message],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage:
        payload_messages: list[dict[str, Any]] = []
        if system:
            payload_messages.append({"role": "system", "content": system})
        payload_messages.extend(self._to_wire(history))

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

        log.info(
            "ollama request model=%s messages=%d tools=%d options=%s",
            self.model,
            len(payload_messages),
            len(tools),
            options or None,
        )
        t0 = time.monotonic()
        try:
            response = _CLIENT.post(self.url, json=payload)
            response.raise_for_status()
        except httpx.HTTPError as e:
            log.exception(
                "ollama request failed after %.2fs model=%s status=%s",
                time.monotonic() - t0,
                self.model,
                getattr(getattr(e, "response", None), "status_code", None),  # noqa: TRY401
            )
            raise
        body = response.json()
        msg = body["message"]
        log.info(
            "ollama response status=%s latency=%.2fs done_reason=%s prompt_tokens=%s eval_tokens=%s",
            getattr(response, "status_code", None),
            time.monotonic() - t0,
            body.get("done_reason"),
            body.get("prompt_eval_count"),
            body.get("eval_count"),
        )
        log.debug("ollama raw response: %.500s", body)

        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc["function"]
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                # Defensive: some Ollama builds emit JSON-string arguments.
                args = json.loads(args) if args.strip() else {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"ollama-{uuid.uuid4().hex[:12]}",
                    name=fn["name"],
                    arguments=args,
                )
            )

        content = msg.get("content", "") or ""
        if not isinstance(content, str):
            # Reject rather than guess: Ollama chat replies are plain text.
            raise TypeError(
                f"ollama: unsupported content type {type(content).__name__}"
            )
        return AssistantMessage(
            text=content,
            tool_calls=tuple(tool_calls),
            usage=Usage(
                body.get("prompt_eval_count") or 0, body.get("eval_count") or 0
            ),
        )


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
        log.exception(
            "ollama: fetch_model_defaults failed url=%s model=%s", show_url, model
        )
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
    log.debug("ollama: model defaults model=%s %s", model, out)
    return out
