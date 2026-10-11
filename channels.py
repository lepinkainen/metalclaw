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

import logging
from collections.abc import Iterable
from typing import Protocol, runtime_checkable


log = logging.getLogger(__name__)


@runtime_checkable
class Channel(Protocol):
    name: str

    async def notify(self, scope: str, text: str) -> None: ...

    def active_scopes(self) -> Iterable[str]: ...


CHANNELS: dict[str, Channel] = {}


def register(channel: Channel) -> None:
    CHANNELS[channel.name] = channel
    log.info("channel registered name=%s (active=%s)", channel.name, sorted(CHANNELS))


def unregister(name: str) -> None:
    removed = CHANNELS.pop(name, None)
    log.info("channel unregistered name=%s found=%s", name, removed is not None)


def for_scope(scope: str) -> Channel | None:
    if scope == "cli":
        ch = CHANNELS.get("cli")
    elif scope.startswith("telegram-"):
        ch = CHANNELS.get("telegram")
    else:
        ch = CHANNELS.get(scope.split("-", 1)[0])
    log.debug("channel for_scope scope=%s -> %s", scope, ch.name if ch else None)
    return ch


def all_active_scopes() -> list[str]:
    scopes: list[str] = []
    for ch in CHANNELS.values():
        scopes.extend(ch.active_scopes())
    return scopes
