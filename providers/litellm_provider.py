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
    flatten_system_prompt,
    split_system_prompt,
)

# Silently strip kwargs the underlying model does not understand. Lets one
# provider module front a heterogeneous Bedrock catalog (Claude, Nova, Llama,
# Mistral) without per-model branching here.
log = logging.getLogger(__name__)

litellm.drop_params = True

_CACHE_CONTROL = {"type": "ephemeral"}


def _is_anthropic_model(model: str) -> bool:
    """True for model strings litellm routes to a Claude model."""
    if model.startswith("anthropic/"):
        return True
    if model.startswith("bedrock/"):
        model_id = model.removeprefix("bedrock/").lower()
        return "anthropic." in model_id or "claude" in model_id
    return False


class LiteLLMProvider:
    name = "litellm"

    def __init__(
        self,
        model: str,
        *,
        aws_region: str | None = None,
        aws_profile: str | None = None,
        num_retries: int = 2,
        prompt_caching: bool = True,
    ) -> None:
        self.model = model
        self.num_retries = num_retries
        # Cache breakpoints are an Anthropic (and Bedrock Claude) feature; other
        # backends get the plain OpenAI-shaped request even though drop_params
        # would strip the unknown keys anyway.
        self.prompt_caching = prompt_caching and _is_anthropic_model(model)
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

    def _system_message(self, system: str) -> dict:
        if not self.prompt_caching:
            return {"role": "system", "content": flatten_system_prompt(system)}
        # List-of-blocks content so the breakpoint sits on a block itself;
        # litellm maps it to Anthropic `system[].cache_control` and to a
        # Bedrock Converse `cachePoint` after that block. Only the stable
        # part is marked: the volatile tail (clock, memory, heartbeat
        # summaries) follows as a plain block so it never invalidates the
        # cached tools + instructions prefix.
        stable, volatile = split_system_prompt(system)
        blocks: list[dict] = [
            {"type": "text", "text": stable, "cache_control": dict(_CACHE_CONTROL)}
        ]
        if volatile:
            blocks.append({"type": "text", "text": volatile})
        return {"role": "system", "content": blocks}

    def _tools(self, tools: list[dict]) -> list[dict]:
        if not self.prompt_caching:
            return tools
        # One breakpoint on the last tool caches the whole tool block (tools
        # precede system in Anthropic's cache order). Copy; the registry's
        # schema dicts are shared with other providers.
        out = list(tools)
        out[-1] = {**out[-1], "cache_control": dict(_CACHE_CONTROL)}
        return out

    def chat_once(
        self,
        history: Sequence[Message],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage:
        payload: list[dict] = []
        if system:
            payload.append(self._system_message(system))
        payload.extend(self._to_wire(history))

        kwargs: dict = {
            "model": self.model,
            "messages": payload,
            "num_retries": self.num_retries,
            **self._extra,
        }
        if tools:
            kwargs["tools"] = self._tools(tools)

        log.info(
            "litellm request model=%s messages=%d tools=%d retries=%d prompt_caching=%s",
            self.model,
            len(payload),
            len(tools),
            self.num_retries,
            self.prompt_caching,
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
        # litellm folds Anthropic's cache_read_input_tokens /
        # cache_creation_input_tokens into prompt_tokens_details.
        details = getattr(usage, "prompt_tokens_details", None)
        cache_read = getattr(details, "cached_tokens", None)
        cache_write = getattr(details, "cache_write_tokens", None)
        if cache_read is not None or cache_write is not None:
            log.debug(
                "litellm prompt cache: read=%s write=%s of prompt_tokens=%s",
                cache_read,
                cache_write,
                getattr(usage, "prompt_tokens", None),
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
                cache_read_tokens=cache_read if isinstance(cache_read, int) else None,
                cache_write_tokens=cache_write
                if isinstance(cache_write, int)
                else None,
            ),
        )
