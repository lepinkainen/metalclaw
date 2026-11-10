"""Heartbeat schedule model and due logic (pure; standard library only).

Action/schedule dataclasses, tool-input validation helpers and the
``is_action_due`` / ``next_due`` checks. No I/O and no config: ledger I/O,
state, the tick runner and the scheduler loop live in ``heartbeat.py``, which
re-exports every name here.
"""

from __future__ import annotations

import enum
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger(__name__)

_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


# --- Action model ---


class ActionKind(enum.StrEnum):
    AT = "at"
    CRON = "cron"
    EVERY = "every"


@dataclass
class CronSchedule:
    days: tuple[str, ...]
    time: str
    timezone: str

    def to_dict(self) -> dict[str, Any]:
        return {"days": list(self.days), "time": self.time, "timezone": self.timezone}


@dataclass
class HeartbeatAction:
    id: str
    kind: ActionKind
    prompt: str
    channel: str
    created_at: str
    created_from: str | None = None
    at: str | None = None
    schedule: CronSchedule | None = None
    every: int | None = None
    # Watcher pipeline name (``pipelines.REGISTRY``) instead of a model prompt.
    # ``kind`` stays the schedule type; ``prompt`` is empty for these.
    pipeline: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": str(self.kind),
            "prompt": self.prompt,
            "channel": self.channel,
            "created_at": self.created_at,
            "created_from": self.created_from,
        }
        if self.pipeline:
            out["pipeline"] = self.pipeline
        if self.kind == ActionKind.AT:
            out["at"] = self.at
        elif self.kind == ActionKind.CRON:
            assert self.schedule is not None
            out["schedule"] = self.schedule.to_dict()
        elif self.kind == ActionKind.EVERY:
            out["every"] = self.every
        return out


# --- Parsing helpers (input validation for tool calls) ---


def parse_interval(text: str | int) -> int:
    """Accept ``30s`` / ``5m`` / ``2h`` / ``1d`` / bare seconds."""
    if isinstance(text, int):
        if text <= 0:
            raise ValueError("interval must be positive")
        return text
    s = str(text).strip()
    if s.isdigit():
        n = int(s)
        if n <= 0:
            raise ValueError("interval must be positive")
        return n
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"invalid interval: {text!r}")
    n = int(m.group(1))
    unit = m.group(2).lower()
    if n <= 0:
        raise ValueError("interval must be positive")
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def normalise_weekdays(days: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    if not days:
        raise ValueError("schedule.days must contain at least one weekday")
    out: list[str] = []
    for raw in days:
        token = str(raw).strip().lower()[:3]
        if token not in _WEEKDAYS:
            raise ValueError(
                f"invalid weekday {raw!r}; use abbreviations like 'mon', 'tue', …"
            )
        if token not in out:
            out.append(token)
    return tuple(out)


def validate_time_string(text: str) -> str:
    if not _TIME_RE.match(text):
        raise ValueError(f"schedule.time must be 'HH:MM' (24h), got {text!r}")
    return text


def validate_timezone(name: str) -> str:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(f"unknown timezone {name!r}") from e
    return name


def parse_iso(text: str) -> datetime:
    """Parse an ISO-8601 timestamp; tz-naive values are treated as *local* time.

    The system prompt hands the model a local wall clock, so a naive
    timestamp it echoes back ("remind me at 16:00" → ``...T16:00:00``) means
    local, not UTC. ``datetime.astimezone()`` on a naive value attaches the
    process-local zone.
    """
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


# --- Due logic ---


def _within_active_hours(now: datetime, window: tuple[int, int] | None) -> bool:
    if window is None:
        return True
    start, end = window
    h = now.hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def is_action_due(
    action: HeartbeatAction, last_run_iso: str | None, now: datetime
) -> bool:
    if action.kind == ActionKind.AT:
        try:
            target = parse_iso(action.at or "")
        except ValueError:
            return False
        if last_run_iso:
            return False
        return now >= target

    if action.kind == ActionKind.EVERY:
        every = action.every or 0
        if not last_run_iso:
            return True
        try:
            last = parse_iso(last_run_iso)
        except ValueError:
            return True
        return (now - last).total_seconds() >= every

    if action.kind == ActionKind.CRON:
        sched = action.schedule
        if sched is None:
            return False
        try:
            tz = ZoneInfo(sched.timezone)
        except ZoneInfoNotFoundError, ValueError:
            return False
        local_now = now.astimezone(tz)
        weekday = _WEEKDAYS[local_now.weekday()]
        if weekday not in sched.days:
            return False
        try:
            h, m = (int(x) for x in sched.time.split(":"))
        except ValueError:
            return False
        scheduled_today = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if local_now < scheduled_today:
            return False
        if last_run_iso:
            try:
                last_local = parse_iso(last_run_iso).astimezone(tz)
            except ValueError:
                return True
            if last_local >= scheduled_today:
                return False
        return True

    return False


def next_due(
    action: HeartbeatAction, last_run_iso: str | None, now: datetime
) -> datetime | None:
    """When ``action`` becomes due next (``now`` if already due); None if never again.

    Actual execution still waits for the next scheduler tick.
    """
    if is_action_due(action, last_run_iso, now):
        return now
    try:
        if action.kind == ActionKind.AT:
            return None if last_run_iso else parse_iso(action.at or "")
        if action.kind == ActionKind.EVERY:
            return parse_iso(last_run_iso or "") + timedelta(seconds=action.every or 0)
        if action.kind == ActionKind.CRON and action.schedule is not None:
            tz = ZoneInfo(action.schedule.timezone)
            h, m = (int(x) for x in action.schedule.time.split(":"))
            today = now.astimezone(tz).date()
            for i in range(8):
                day = today + timedelta(days=i)
                cand = datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                if _WEEKDAYS[cand.weekday()] in action.schedule.days and cand > now:
                    return cand
    except ValueError, ZoneInfoNotFoundError:
        log.warning("next_due: bad schedule for action %s", action.id)
    return None
