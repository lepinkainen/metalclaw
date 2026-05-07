import json

from openai import OpenAI

from providers.base import AssistantMessage, ToolCall


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        self._client = OpenAI(api_key=api_key)
        self.model = model

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage:
        payload: list[dict] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(messages)

        kwargs: dict = {"model": self.model, "messages": payload}
        if tools:
            kwargs["tools"] = tools

        resp = self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict] = []
        for tc in msg.tool_calls or []:
            args_str = tc.function.arguments or ""
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
            raw_tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": args_str},
            })

        raw: dict = {"role": "assistant", "content": msg.content or ""}
        if raw_tool_calls:
            raw["tool_calls"] = raw_tool_calls

        return AssistantMessage(
            text=msg.content or "",
            tool_calls=tool_calls,
            raw=raw,
        )

    def format_tool_results(
        self,
        results: list[tuple[ToolCall, str]],
    ) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": call.id, "content": result_json}
            for call, result_json in results
        ]
