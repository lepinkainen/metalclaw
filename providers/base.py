from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class AssistantMessage:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict | list[dict] | None = None


class Provider(Protocol):
    """Provider-agnostic chat surface.

    Implementations own their own native message shape — only `messages`
    coming back from `format_tool_results` and the `raw` field of
    `AssistantMessage` are appended to history. Each session sticks to one
    provider, so the rest of the app treats history as opaque.
    """

    name: str

    def chat_once(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AssistantMessage: ...

    def format_tool_results(
        self,
        results: list[tuple[ToolCall, str]],
    ) -> list[dict]: ...
