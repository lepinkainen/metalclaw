"""Shared plumbing for watcher pipelines (see ``ai-docs/mail-pipeline-plan.md``).

A pipeline is an ordinary ``async def run(ctx: PipelineContext) -> Outcome``.
Everything a pipeline needs from the host — config, the shared ledger, the
model, the delivery channel, tracing — arrives through ``PipelineContext``;
``heartbeat.py`` builds one per run and persists ``ctx.trace`` in a
``finally`` block. No step registry, no graph: branching is ``if``/``return``.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import history
from config import Config

log = logging.getLogger("metalclaw.pipelines")


class Outcome(enum.StrEnum):
    SUPPRESSED = "suppressed"  # nothing survived the code filters; zero tokens
    DISMISSED = "dismissed"  # the model saw the batch and answered HEARTBEAT_OK
    DELIVERED = "delivered"  # a summary reached the channel


@dataclass
class Step:
    name: str
    ms: int = 0
    note: str | None = None

    def set_note(self, text: str) -> None:
        self.note = text


@dataclass
class Trace:
    """Per-run step list; deliberately boring (a list, persisted by the runner)."""

    steps: list[Step] = field(default_factory=list)

    @contextmanager
    def step(self, name: str) -> Generator[Step]:
        st = Step(name=name)
        self.steps.append(st)
        t0 = time.monotonic()
        try:
            yield st
        finally:
            st.ms = int((time.monotonic() - t0) * 1000)
            log.debug("pipeline step %s %dms %s", name, st.ms, st.note or "")

    def as_rows(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "ms": s.ms, **({"note": s.note} if s.note else {})}
            for s in self.steps
        ]


class Ledger:
    """The shared ``reported_items`` table, scoped by ``source`` per call."""

    def drop_reported(
        self, source: str, items: Sequence[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        seen = history.reported_ids(source, [str(i["id"]) for i in items])
        return [i for i in items if str(i["id"]) not in seen]

    def mark(self, source: str, items: Sequence[dict[str, Any]], outcome: str) -> int:
        return history.reported_mark(source, [str(i["id"]) for i in items], outcome)


AskModel = Callable[..., Awaitable[str]]
Deliver = Callable[[str], Awaitable[None]]


@dataclass
class PipelineContext:
    config: Config
    now: datetime
    channel: str | None
    trace: Trace
    ledger: Ledger
    ask_model: AskModel
    """``await ctx.ask_model(system=..., user=..., tools={...}) -> str``: one
    ``chat_loop.chat`` call on a scope-less session, off the event loop."""
    deliver: Deliver
    """``await ctx.deliver(text)``: send to ``channel``; raises on failure."""


def default_ask_model(now_text: str) -> AskModel:
    """``ask_model`` that runs ``chat_loop.chat`` in a worker thread."""

    async def _ask(*, system: str, user: str, tools: set[str] | frozenset[str]) -> str:
        import chat_loop  # local: chat_loop imports heartbeat at module load

        session = chat_loop.Session(
            system=chat_loop.build_system_prompt(now_text) + "\n\n" + system
        )
        session.append_user(user)
        return await asyncio.to_thread(
            chat_loop.chat, session, only_tools=frozenset(tools)
        )

    return _ask
