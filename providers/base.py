from collections.abc import Sequence
from typing import Protocol

from messages import AssistantMessage, Message, ToolCall, Usage

__all__ = ["AssistantMessage", "Message", "Provider", "ToolCall", "Usage"]


class Provider(Protocol):
    """Provider-agnostic chat surface.

    ``history`` is canonical ``messages.Message`` objects. Each implementation
    converts them to its own wire shape privately and parses its response
    into an ``AssistantMessage``; no provider-specific dict ever reaches the
    caller, so sessions can switch provider mid-conversation.
    """

    name: str

    def chat_once(
        self,
        history: Sequence[Message],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage: ...
