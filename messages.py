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
    "AssistantMessage",
    "Message",
    "ToolCall",
    "ToolResultMessage",
    "Usage",
    "UserMessage",
    "validate_exchange",
]


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
