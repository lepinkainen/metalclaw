"""Canonical, provider-agnostic conversation messages.

Session history holds only these frozen dataclasses. Each provider converts
them to its own wire format on the way out and parses its response into an
``AssistantMessage`` on the way in, so history never carries a foreign
payload and the active provider can change mid-session without a reset.

No project imports: importable by ``providers``, ``chat_loop``, ``heartbeat``
and tests alike.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

__all__ = [
    "SYSTEM_CACHE_BOUNDARY",
    "AssistantMessage",
    "Message",
    "ToolCall",
    "ToolResultMessage",
    "Usage",
    "UserMessage",
    "flatten_system_prompt",
    "message_from_dict",
    "message_to_dict",
    "split_system_prompt",
    "validate_exchange",
]

# Separates the stable part of a system prompt (instructions, tool notes)
# from the volatile tail (clock, memory and heartbeat summaries). Providers
# that support prompt caching put the cache breakpoint on the stable part
# only; everyone else flattens the two back into one string.
SYSTEM_CACHE_BOUNDARY = "\n\x1e\n"


def split_system_prompt(system: str) -> tuple[str, str]:
    """``(stable, volatile)``; ``volatile`` is ``""`` when there is no boundary."""
    stable, sep, volatile = system.partition(SYSTEM_CACHE_BOUNDARY)
    return (stable, volatile) if sep else (system, "")


def flatten_system_prompt(system: str) -> str:
    """One plain string for providers without a cache breakpoint."""
    return system.replace(SYSTEM_CACHE_BOUNDARY, "\n\n")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None = None  # None = unknown (local model / unpriced)
    # Prompt-cache breakdown of prompt_tokens; None = provider reports none.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None


@dataclass(frozen=True)
class UserMessage:
    content: str


@dataclass(frozen=True)
class AssistantMessage:
    """One assistant turn. ``usage`` is bookkeeping, never serialised and
    ignored for equality."""

    text: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        # Accept any sequence for ergonomics; store an immutable tuple.
        object.__setattr__(self, "tool_calls", tuple(self.tool_calls))


@dataclass(frozen=True)
class ToolResultMessage:
    call_id: str
    name: str
    content: str  # already JSON-encoded


Message = UserMessage | AssistantMessage | ToolResultMessage


def validate_exchange(
    assistant: AssistantMessage, results: Sequence[ToolResultMessage]
) -> None:
    """Raise ``ValueError`` unless ``results`` answer exactly ``assistant.tool_calls``.

    Every provider rejects a tool call without a result (or the reverse), so
    the pairing is enforced here, at the one place history grows by a round.
    """
    call_ids = [tc.id for tc in assistant.tool_calls]
    if len(set(call_ids)) != len(call_ids):
        raise ValueError(f"duplicate tool_call ids in assistant turn: {call_ids}")
    result_ids = [r.call_id for r in results]
    if len(set(result_ids)) != len(result_ids):
        raise ValueError(f"duplicate tool result ids: {result_ids}")
    missing = set(call_ids) - set(result_ids)
    unexpected = set(result_ids) - set(call_ids)
    if missing or unexpected:
        raise ValueError(
            "tool results do not match tool calls: "
            f"missing={sorted(missing)} unexpected={sorted(unexpected)}"
        )


def message_to_dict(message: Message) -> dict:
    """JSON-ready dict with a ``kind`` tag; ``Usage`` is bookkeeping and dropped."""
    match message:
        case UserMessage(content=content):
            return {"kind": "user", "content": content}
        case AssistantMessage(text=text, tool_calls=tool_calls):
            return {
                "kind": "assistant",
                "text": text,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in tool_calls
                ],
            }
        case ToolResultMessage(call_id=call_id, name=name, content=content):
            return {
                "kind": "tool_result",
                "call_id": call_id,
                "name": name,
                "content": content,
            }
    raise TypeError(f"not a Message: {type(message).__name__}")


def message_from_dict(data: dict) -> Message:
    """Inverse of ``message_to_dict``; raises ``ValueError`` on an unknown kind."""
    match data.get("kind"):
        case "user":
            return UserMessage(data["content"])
        case "assistant":
            return AssistantMessage(
                text=data.get("text", ""),
                tool_calls=tuple(
                    ToolCall(tc["id"], tc["name"], dict(tc.get("arguments") or {}))
                    for tc in data.get("tool_calls", ())
                ),
            )
        case "tool_result":
            return ToolResultMessage(data["call_id"], data["name"], data["content"])
    raise ValueError(f"unknown message kind: {data.get('kind')!r}")
