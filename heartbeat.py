"""Heartbeat scheduler.

Periodic asyncio loop that wakes Metalclaw, picks each due action from a
bot-owned YAML ledger at ``<memory_dir>/heartbeat.yaml``, runs ``chat_loop.chat()``
with the action's prompt, and routes any non-``HEARTBEAT_OK`` reply to the
channel pinned on the action (or the configured default).

Actions are created/listed/cancelled via the model-facing tools
``create_heartbeat_action`` / ``list_heartbeat_actions`` /
``cancel_heartbeat_action`` (see ``tools/heartbeat_tools.py``). Users do not
edit this file by hand.

Three action kinds:
  * ``at``     — one-shot, fires once after ``at`` timestamp; archived to
                 ``completed`` after a successful notify.
  * ``cron``   — calendar-recurring on selected weekdays at one local time
                 in a given timezone.
  * ``every``  — interval-recurring; fires whenever ``every`` seconds have
                 elapsed since the last successful run.

Per-action last-run state lives at
``$XDG_DATA_HOME/metalclaw/heartbeat_state.json`` keyed by action id.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import fcntl
import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

import channels
from config import get_config

log = logging.getLogger("metalclaw.heartbeat")

SENTINEL = "HEARTBEAT_OK"
_STATE_VERSION = 1
_LEDGER_VERSION = 1
_LEDGER_FILENAME = "heartbeat.yaml"
_LEDGER_LOCK_FILENAME = "heartbeat.yaml.lock"
_COMPLETED_CAP = 50
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

_LEDGER_PROCESS_LOCK = threading.Lock()


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

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": str(self.kind),
            "prompt": self.prompt,
            "channel": self.channel,
            "created_at": self.created_at,
            "created_from": self.created_from,
        }
        if self.kind == ActionKind.AT:
            out["at"] = self.at
        elif self.kind == ActionKind.CRON:
            assert self.schedule is not None
            out["schedule"] = self.schedule.to_dict()
        elif self.kind == ActionKind.EVERY:
            out["every"] = self.every
        return out


@dataclass
class HeartbeatLedger:
    actions: list[HeartbeatAction] = field(default_factory=list)
    completed: list[dict[str, Any]] = field(default_factory=list)


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
    """Parse an ISO-8601 timestamp; tz-naive treated as UTC."""
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- Ledger I/O ---


def _ledger_path() -> Path:
    cfg = get_config()
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    return cfg.memory_dir / _LEDGER_FILENAME


def _lock_path() -> Path:
    cfg = get_config()
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    return cfg.memory_dir / _LEDGER_LOCK_FILENAME


@contextlib.contextmanager
def _ledger_lock():
    """Process + cross-process exclusive lock on the ledger sidecar."""
    lp = _lock_path()
    with _LEDGER_PROCESS_LOCK:
        with open(lp, "w") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _action_from_dict(raw: dict[str, Any]) -> HeartbeatAction:
    kind = ActionKind(str(raw["kind"]))
    schedule = None
    if kind == ActionKind.CRON:
        sd = raw.get("schedule") or {}
        schedule = CronSchedule(
            days=normalise_weekdays(sd.get("days") or []),
            time=validate_time_string(str(sd.get("time", ""))),
            timezone=validate_timezone(str(sd.get("timezone", ""))),
        )
    return HeartbeatAction(
        id=str(raw["id"]),
        kind=kind,
        prompt=str(raw.get("prompt", "")),
        channel=str(raw.get("channel", "")),
        created_at=str(raw.get("created_at", _now_iso())),
        created_from=raw.get("created_from"),
        at=str(raw["at"]) if kind == ActionKind.AT else None,
        schedule=schedule,
        every=int(raw["every"]) if kind == ActionKind.EVERY else None,
    )


def _parse_ledger(text: str) -> HeartbeatLedger:
    if not text.strip():
        return HeartbeatLedger()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"heartbeat.yaml parse error: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("heartbeat.yaml top-level must be a mapping")
    raw_actions = data.get("actions") or []
    raw_completed = data.get("completed") or []
    actions = [_action_from_dict(a) for a in raw_actions if isinstance(a, dict)]
    completed = [c for c in raw_completed if isinstance(c, dict)]
    return HeartbeatLedger(actions=actions, completed=completed)


def _render_ledger(ledger: HeartbeatLedger) -> str:
    payload = {
        "version": _LEDGER_VERSION,
        "actions": [a.to_dict() for a in ledger.actions],
        "completed": list(ledger.completed),
    }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)


def _read_ledger_locked() -> HeartbeatLedger:
    p = _ledger_path()
    if not p.exists():
        return HeartbeatLedger()
    return _parse_ledger(p.read_text(encoding="utf-8"))


def _write_ledger_locked(ledger: HeartbeatLedger) -> None:
    """Atomic write: tempfile in same dir, then ``os.replace``."""
    p = _ledger_path()
    text = _render_ledger(ledger)
    fd, tmp = tempfile.mkstemp(prefix=".heartbeat-", suffix=".yaml", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def load_ledger() -> HeartbeatLedger:
    with _ledger_lock():
        return _read_ledger_locked()


# --- Mutators ---


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _generate_id(existing: set[str]) -> str:
    for _ in range(20):
        candidate = secrets.token_hex(3)
        if candidate not in existing:
            return candidate
    raise RuntimeError("could not allocate unique heartbeat action id after 20 tries")


def create_action(
    *,
    kind: ActionKind,
    prompt: str,
    channel: str,
    created_from: str | None,
    at: str | None = None,
    schedule: CronSchedule | None = None,
    every: int | None = None,
) -> HeartbeatAction:
    """Append a new action to the ledger and return it.

    Validation matches ``ActionKind`` requirements:
      * ``at``     — ``at`` (ISO-8601) required
      * ``cron``   — ``schedule`` required
      * ``every``  — ``every`` (positive seconds) required
    """
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not channel.strip():
        raise ValueError("channel must be set (no default channel resolvable)")

    if kind == ActionKind.AT:
        if not at:
            raise ValueError("kind=at requires 'at' (ISO-8601 timestamp)")
        parse_iso(at)
        schedule = None
        every = None
    elif kind == ActionKind.CRON:
        if schedule is None:
            raise ValueError("kind=cron requires schedule.days/time/timezone")
        at = None
        every = None
    elif kind == ActionKind.EVERY:
        if every is None:
            raise ValueError("kind=every requires 'every' (seconds)")
        if every <= 0:
            raise ValueError("'every' must be positive")
        at = None
        schedule = None
    else:
        raise ValueError(f"unknown kind: {kind!r}")

    with _ledger_lock():
        ledger = _read_ledger_locked()
        existing_ids = {a.id for a in ledger.actions}
        existing_ids.update(c.get("id", "") for c in ledger.completed)
        action = HeartbeatAction(
            id=_generate_id(existing_ids),
            kind=kind,
            prompt=prompt.strip(),
            channel=channel,
            created_at=_now_iso(),
            created_from=created_from,
            at=at,
            schedule=schedule,
            every=every,
        )
        ledger.actions.append(action)
        _write_ledger_locked(ledger)
    log.info(
        "heartbeat action created id=%s kind=%s channel=%s",
        action.id, action.kind, action.channel,
    )
    return action


def list_active() -> list[HeartbeatAction]:
    return load_ledger().actions


def list_completed() -> list[dict[str, Any]]:
    return load_ledger().completed


def cancel(action_id: str) -> HeartbeatAction | None:
    with _ledger_lock():
        ledger = _read_ledger_locked()
        for i, a in enumerate(ledger.actions):
            if a.id == action_id:
                removed = ledger.actions.pop(i)
                _write_ledger_locked(ledger)
                log.info("heartbeat action cancelled id=%s", action_id)
                return removed
        return None


def _archive_completed(action: HeartbeatAction, ledger: HeartbeatLedger, completed_at: str) -> None:
    snapshot = action.to_dict()
    snapshot["completed_at"] = completed_at
    ledger.completed.append(snapshot)
    if len(ledger.completed) > _COMPLETED_CAP:
        ledger.completed = ledger.completed[-_COMPLETED_CAP:]


# --- State (last-run timestamps, keyed by action id) ---


def _state_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    p = Path(xdg) / "metalclaw" / "heartbeat_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_state() -> dict[str, str]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("heartbeat state unreadable, starting fresh: %s", e)
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw.get("last_run", {}) if "last_run" in raw else raw


def save_state(state: dict[str, str]) -> None:
    payload = {"version": _STATE_VERSION, "last_run": state}
    _state_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


# --- Due logic ---


def _within_active_hours(now: datetime, window: tuple[int, int] | None) -> bool:
    if window is None:
        return True
    start, end = window
    h = now.hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def is_action_due(action: HeartbeatAction, last_run_iso: str | None, now: datetime) -> bool:
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
        except (ZoneInfoNotFoundError, ValueError):
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


# --- Tick logic ---


def _build_action_messages(action: HeartbeatAction, now: datetime, build_system_prompt) -> list[dict]:
    local = now.astimezone() if now.tzinfo else now
    now_str = local.strftime("%Y-%m-%d %H:%M")
    system = build_system_prompt(now_str)
    system += (
        "\n\nHEARTBEAT MODE: This is a scheduled wake-up, not a user message. "
        "Run only the action below. Use tools as needed. "
        f"If nothing requires the user's attention, reply with exactly `{SENTINEL}` "
        "and nothing else. Otherwise, reply with a concise alert the user should see."
    )
    user_lines = [
        f"Heartbeat action **{action.id}** ({action.kind}) is due.",
        "",
        action.prompt,
    ]
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def _resolve_channel(action_channel: str | None) -> tuple[str | None, channels.Channel | None]:
    cfg = get_config()
    target = action_channel or cfg.heartbeat_default_channel
    if not target:
        return None, None
    return target, channels.for_scope(target)


async def _run_action(action: HeartbeatAction, now: datetime, chat_fn, build_system_prompt) -> str:
    messages = _build_action_messages(action, now, build_system_prompt)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: chat_fn(messages))


async def run_tick(*, now: datetime | None = None) -> dict[str, str]:
    """Single heartbeat tick across all due actions. Returns ``{action_id: reply}``.

    A reply of ``""`` indicates ``HEARTBEAT_OK``-suppression. A successful
    one-shot (``kind=at``) action is moved to ``completed`` only after the
    notify call succeeds — failures keep the action active.
    """
    import chat_loop  # local to avoid circular import at module load

    now = now or datetime.now(timezone.utc)
    cfg = get_config()
    if not _within_active_hours(now.astimezone(), cfg.heartbeat_active_hours):
        log.debug("heartbeat tick outside active hours")
        return {}

    state = load_state()
    snapshot = load_ledger()
    replies: dict[str, str] = {}

    for action in list(snapshot.actions):
        last_run_iso = state.get(action.id)
        try:
            due = is_action_due(action, last_run_iso, now)
        except Exception:
            log.exception("heartbeat action %s: due-check crashed", action.id)
            continue
        if not due:
            continue

        try:
            reply = await _run_action(action, now, chat_loop.chat, chat_loop.build_system_prompt)
        except Exception as e:
            log.exception("heartbeat action %s: chat turn failed: %s", action.id, e)
            continue

        clean = reply.strip()
        if clean == SENTINEL or clean.startswith(SENTINEL):
            replies[action.id] = ""
            state[action.id] = now.isoformat()
            if action.kind == ActionKind.AT:
                # one-shots that ack with HEARTBEAT_OK are still "done"
                _archive_with_lock(action.id, now)
            continue

        target_scope, channel = _resolve_channel(action.channel)
        if channel is None:
            log.warning(
                "heartbeat action %s: no channel resolves for %r — keeping active",
                action.id, target_scope,
            )
            continue

        try:
            await channel.notify(target_scope or "", clean)
        except Exception as e:
            log.exception(
                "heartbeat action %s: channel notify failed: %s — keeping active",
                action.id, e,
            )
            continue

        replies[action.id] = clean
        state[action.id] = now.isoformat()
        if action.kind == ActionKind.AT:
            _archive_with_lock(action.id, now)

    save_state(state)
    return replies


def _archive_with_lock(action_id: str, now: datetime) -> None:
    completed_at = now.isoformat()
    with _ledger_lock():
        ledger = _read_ledger_locked()
        for i, a in enumerate(ledger.actions):
            if a.id == action_id:
                removed = ledger.actions.pop(i)
                _archive_completed(removed, ledger, completed_at)
                _write_ledger_locked(ledger)
                return


# --- System-prompt summary ---


def summary(max_chars: int = 400) -> str:
    """Compact summary of active actions for system-prompt injection."""
    try:
        actions = list_active()
    except Exception:
        return ""
    if not actions:
        return ""
    parts: list[str] = []
    for a in actions:
        if a.kind == ActionKind.AT:
            tail = f"at {a.at}"
        elif a.kind == ActionKind.CRON and a.schedule is not None:
            tail = f"{','.join(a.schedule.days)} {a.schedule.time} {a.schedule.timezone}"
        elif a.kind == ActionKind.EVERY:
            tail = f"every {a.every}s"
        else:
            tail = ""
        parts.append(f"{a.id} [{a.kind}] → {a.channel} | {tail} | {a.prompt[:80]}")
    text = "\n".join(parts)
    if len(text) > max_chars:
        hint = " (call list_heartbeat_actions for full list)"
        text = text[: max(0, max_chars - len(hint) - 1)].rstrip() + "…" + hint
    return text


# --- Scheduler loop ---


async def run(stop: asyncio.Event) -> None:
    """Main loop: tick, sleep interval, repeat until ``stop`` is set."""
    cfg = get_config()
    if not cfg.heartbeat_enabled:
        log.info("heartbeat disabled in config")
        return

    interval = max(30, cfg.heartbeat_interval_seconds)
    log.info("heartbeat loop started, interval=%ds", interval)
    while not stop.is_set():
        try:
            await run_tick()
        except Exception as e:
            log.exception("heartbeat tick crashed: %s", e)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("heartbeat loop stopped")
