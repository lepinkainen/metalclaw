import json

import litellm

from providers.base import AssistantMessage, ToolCall


# Silently strip kwargs the underlying model does not understand. Lets one
# provider module front a heterogeneous Bedrock catalog (Claude, Nova, Llama,
# Mistral) without per-model branching here.
litellm.drop_params = True


class LiteLLMProvider:
    name = "litellm"

    def __init__(
        self,
        model: str,
        *,
        aws_region: str | None = None,
        aws_profile: str | None = None,
        num_retries: int = 2,
    ) -> None:
        self.model = model
        self.num_retries = num_retries
        self._extra: dict = {}
        if aws_region:
            self._extra["aws_region_name"] = aws_region
        if aws_profile:
            self._extra["aws_profile_name"] = aws_profile

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

        kwargs: dict = {
            "model": self.model,
            "messages": payload,
            "num_retries": self.num_retries,
            **self._extra,
        }
        if tools:
            kwargs["tools"] = tools

        resp = litellm.completion(**kwargs)
        msg = resp.choices[0].message

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
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

        content = getattr(msg, "content", "") or ""
        raw: dict = {"role": "assistant", "content": content}
        if raw_tool_calls:
            raw["tool_calls"] = raw_tool_calls

        return AssistantMessage(text=content, tool_calls=tool_calls, raw=raw)

    def format_tool_results(
        self,
        results: list[tuple[ToolCall, str]],
    ) -> list[dict]:
        return [
            {"role": "tool", "tool_call_id": call.id, "content": result_json}
            for call, result_json in results
        ]
