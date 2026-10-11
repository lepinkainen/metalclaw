import json
import logging
import time

import litellm

from providers.base import AssistantMessage, ToolCall

# Silently strip kwargs the underlying model does not understand. Lets one
# provider module front a heterogeneous Bedrock catalog (Claude, Nova, Llama,
# Mistral) without per-model branching here.
log = logging.getLogger(__name__)

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

        log.info(
            "litellm request model=%s messages=%d tools=%d retries=%d",
            self.model, len(payload), len(tools), self.num_retries,
        )
        t0 = time.monotonic()
        try:
            resp = litellm.completion(**kwargs)
        except Exception:
            log.exception(
                "litellm request failed after %.2fs model=%s", time.monotonic() - t0, self.model
            )
            raise
        usage = getattr(resp, "usage", None)
        try:
            cost = litellm.completion_cost(completion_response=resp)
        except Exception:  # noqa: BLE001
            cost = None  # model missing from litellm's price map
        log.info(
            "litellm response latency=%.2fs finish_reason=%s prompt_tokens=%s completion_tokens=%s cost_usd=%s",
            time.monotonic() - t0,
            getattr(resp.choices[0], "finish_reason", None),
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
            f"{cost:.6f}" if cost is not None else None,
        )
        log.debug("litellm raw response: %.500s", resp)
        msg = resp.choices[0].message

        tool_calls: list[ToolCall] = []
        raw_tool_calls: list[dict] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            args_str = tc.function.arguments or ""
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                log.warning("litellm: bad tool-call JSON for %s: %.200s", tc.function.name, args_str)
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
