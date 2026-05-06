"""Frontend-agnostic notification channels.

Each frontend (CLI, Telegram, future) registers a `Channel` at startup. The
heartbeat (and any other proactive surface) calls `for_scope(scope).notify(...)`
to deliver a message without knowing the transport.

Scope convention:
  - "cli"                 → CLI channel
  - "telegram-<chat_id>"  → Telegram channel
  - "<frontend>-<id>"     → that frontend's channel
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable


@runtime_checkable
class Channel(Protocol):
    name: str

    async def notify(self, scope: str, text: str) -> None: ...

    def active_scopes(self) -> Iterable[str]: ...


CHANNELS: dict[str, Channel] = {}


def register(channel: Channel) -> None:
    CHANNELS[channel.name] = channel


def unregister(name: str) -> None:
    CHANNELS.pop(name, None)


def for_scope(scope: str) -> Channel | None:
    if scope == "cli":
        return CHANNELS.get("cli")
    if scope.startswith("telegram-"):
        return CHANNELS.get("telegram")
    prefix = scope.split("-", 1)[0]
    return CHANNELS.get(prefix)


def all_active_scopes() -> list[str]:
    scopes: list[str] = []
    for ch in CHANNELS.values():
        scopes.extend(ch.active_scopes())
    return scopes
