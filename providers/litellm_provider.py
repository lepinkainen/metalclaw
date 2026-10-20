import json
import logging
import time
from collections.abc import Sequence

import litellm

from messages import (
    AssistantMessage,
    Message,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)

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

    @staticmethod
    def _to_wire(history: Sequence[Message]) -> list[dict]:
        """Canonical messages -> OpenAI-shaped chat messages (litellm envelope)."""
        out: list[dict] = []
        for m in history:
            if isinstance(m, UserMessage):
                out.append({"role": "user", "content": m.content})
            elif isinstance(m, AssistantMessage):
                entry: dict = {"role": "assistant", "content": m.text}
                if m.tool_calls:
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments),
                            },
                        }
                        for tc in m.tool_calls
                    ]
                out.append(entry)
            elif isinstance(m, ToolResultMessage):
                out.append(
                    {"role": "tool", "tool_call_id": m.call_id, "content": m.content}
                )
            else:  # pragma: no cover - exhaustiveness guard
                raise TypeError(f"unsupported message type: {type(m).__name__}")
        return out

    def chat_once(
        self,
        history: Sequence[Message],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage:
        payload: list[dict] = []
        if system:
            payload.append({"role": "system", "content": system})
        payload.extend(self._to_wire(history))

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
            self.model,
            len(payload),
            len(tools),
            self.num_retries,
        )
        t0 = time.monotonic()
        try:
            resp = litellm.completion(**kwargs)
        except Exception:
            log.exception(
                "litellm request failed after %.2fs model=%s",
                time.monotonic() - t0,
                self.model,
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
        for tc in getattr(msg, "tool_calls", None) or []:
            args_str = tc.function.arguments or ""
            try:
                args = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                log.warning(
                    "litellm: bad tool-call JSON for %s: %.200s",
                    tc.function.name,
                    args_str,
                )
                args = {}
            tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))

        content = getattr(msg, "content", "") or ""
        if isinstance(content, list):
            # Content-block responses: keep text blocks, reject anything else.
            texts = []
            for block in content:
                btype = block.get("type") if isinstance(block, dict) else None
                if btype == "text":
                    texts.append(block.get("text", ""))
                else:
                    raise ValueError(f"litellm: unsupported content block {btype!r}")
            content = "".join(texts)
        elif not isinstance(content, str):
            raise TypeError(
                f"litellm: unsupported content type {type(content).__name__}"
            )

        return AssistantMessage(
            text=content,
            tool_calls=tuple(tool_calls),
            usage=Usage(
                getattr(usage, "prompt_tokens", None) or 0,
                getattr(usage, "completion_tokens", None) or 0,
                cost,
            ),
        )
