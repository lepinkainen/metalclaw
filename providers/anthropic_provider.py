from anthropic import Anthropic

from providers.base import AssistantMessage, ToolCall


_MAX_TOKENS = 4096


def _to_anthropic_tools(tools: list[dict]) -> list[dict]:
    """Translate registry tool schemas to Anthropic's input_schema shape."""
    out: list[dict] = []
    for t in tools:
        fn = t.get("function") or {}
        out.append({
            "name": fn.get("name") or t.get("name"),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str) -> None:
        self._client = Anthropic(api_key=api_key)
        self.model = model

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

        resp = self._client.messages.create(**kwargs)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        raw_blocks: list[dict] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text = getattr(block, "text", "") or ""
                text_parts.append(text)
                raw_blocks.append({"type": "text", "text": text})
            elif btype == "tool_use":
                tid = block.id
                name = block.name
                args = block.input or {}
                tool_calls.append(ToolCall(id=tid, name=name, arguments=dict(args)))
                raw_blocks.append({
                    "type": "tool_use",
                    "id": tid,
                    "name": name,
                    "input": dict(args),
                })

        raw = {"role": "assistant", "content": raw_blocks}
        return AssistantMessage(
            text="\n".join(p for p in text_parts if p),
            tool_calls=tool_calls,
            raw=raw,
        )

    def format_tool_results(
        self,
        results: list[tuple[ToolCall, str]],
    ) -> list[dict]:
        blocks = [
            {"type": "tool_result", "tool_use_id": call.id, "content": result_json}
            for call, result_json in results
        ]
        return [{"role": "user", "content": blocks}]
