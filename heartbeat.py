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
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

import channels
import history as history_db
from config import get_config

if TYPE_CHECKING:
    import chat_loop

log = logging.getLogger("metalclaw.heartbeat")

SENTINEL = "HEARTBEAT_OK"
# Models often wrap the sentinel in a "nothing to report" sentence; treat short
# leftovers as an ack. Longer leftovers are a real alert with a stray sentinel.
_ACK_MAX_CHARS = 300
_STATE_VERSION = 2
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


def format_now(dt: datetime | None = None) -> str:
    """Local wall clock *with* UTC offset, for the system prompt.

    Shared by every frontend's session bootstrap and the heartbeat prompt so
    the model always sees e.g. ``2026-10-20 15:30 EEST (UTC+0300)`` and has
    enough context to emit tz-aware ``at`` timestamps.
    """
    local = (dt or datetime.now()).astimezone()
    return local.strftime("%Y-%m-%d %H:%M %Z (UTC%z)")


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
    with _LEDGER_PROCESS_LOCK, open(lp, "w") as f:
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
        raise ValueError("heartbeat.yaml top-level must be a mapping")  # noqa: TRY004 callers catch ValueError
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
    return datetime.now(UTC).isoformat(timespec="seconds")


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
        existing_ids.update(load_state())  # never recycle an id with live state
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
        action.id,
        action.kind,
        action.channel,
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
                forget_state(action_id)
                return removed
        return None


def _archive_completed(
    action: HeartbeatAction, ledger: HeartbeatLedger, completed_at: str
) -> None:
    snapshot = action.to_dict()
    snapshot["completed_at"] = completed_at
    ledger.completed.append(snapshot)
    if len(ledger.completed) > _COMPLETED_CAP:
        ledger.completed = ledger.completed[-_COMPLETED_CAP:]


# --- State (per-action scheduler state in history.db, keyed by action id) ---
#
# Rows live in the ``heartbeat_state`` table of ``$XDG_DATA_HOME/metalclaw/history.db``
# (see history.py). Per-row upserts mean two processes (``--daemon`` + an
# interactive ``bot.py``) or a manual ``/heartbeat run`` racing the scheduler
# can never lose each other's writes — unlike the old whole-file JSON.


@dataclass
class ActionState:
    """One ``heartbeat_state`` row.

    ``last_run`` drives the due logic; ``failure_count`` / ``last_failed`` drive
    exponential backoff so an action whose run or delivery keeps failing
    (channel unset, gateway down, empty reply) is not re-run with a fresh model
    call on every tick. ``meta`` is free-form JSON for after-the-fact debugging
    (merged on every write; see ``_meta_for``). A success clears the failures.
    """

    action_id: str
    last_run: str | None = None
    failure_count: int = 0
    last_failed: str | None = None
    updated_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: history_db.HeartbeatStateRow) -> ActionState:
        action_id, last_run, failure_count, last_failed, updated_at, meta_json = row
        try:
            meta = json.loads(meta_json or "{}")
        except json.JSONDecodeError:
            meta = {"_corrupt_meta": meta_json}
        return cls(
            action_id=action_id,
            last_run=last_run,
            failure_count=int(failure_count or 0),
            last_failed=last_failed,
            updated_at=updated_at or "",
            meta=meta if isinstance(meta, dict) else {"_meta": meta},
        )

    def retry_at(self) -> datetime | None:
        """Earliest time the action may run again after failures; None if clear."""
        if self.failure_count <= 0 or not self.last_failed:
            return None
        try:
            failed = parse_iso(self.last_failed)
        except ValueError:
            return None
        return failed + timedelta(seconds=backoff_seconds(self.failure_count))


def backoff_seconds(failure_count: int) -> int:
    """Delay before retrying after ``failure_count`` consecutive failures.

    ``interval * 2**(count-1)`` capped at ``heartbeat_failure_backoff_max_seconds``:
    the first failure retries on the next tick, then every 2nd, 4th, … tick,
    up to the cap (default 1h). Network blips recover fast; a persistent
    misconfiguration (e.g. ``discord_heartbeat_channel`` unset) stops
    burning a model call per tick while still surfacing a warning.
    """
    cfg = get_config()
    base = max(cfg.heartbeat_interval_seconds, 1)
    cap = max(cfg.heartbeat_failure_backoff_max_seconds, base)
    exp = min(max(failure_count, 1) - 1, 30)  # avoid huge shifts
    return min(base * (2**exp), cap)


def _meta_for(action: HeartbeatAction, **extra: Any) -> dict[str, Any]:
    """Debug metadata stored alongside the state row (merged on each write)."""
    meta: dict[str, Any] = {
        "kind": str(action.kind),
        "channel": action.channel,
        "prompt": action.prompt[:120],
    }
    if action.kind == ActionKind.AT:
        meta["at"] = action.at
    elif action.kind == ActionKind.EVERY:
        meta["every"] = action.every
    elif action.schedule is not None:
        meta["schedule"] = action.schedule.to_dict()
    meta.update(extra)
    return meta


def load_state() -> dict[str, ActionState]:
    """All state rows keyed by action id (runs the one-time JSON migration)."""
    _migrate_legacy_state_file()
    return {
        row[0]: ActionState.from_row(row) for row in history_db.heartbeat_state_all()
    }


def get_state(action_id: str) -> ActionState | None:
    _migrate_legacy_state_file()
    row = history_db.heartbeat_state_get(action_id)
    return ActionState.from_row(row) if row is not None else None


def mark_success(action: HeartbeatAction, now: datetime, **extra: Any) -> None:
    """Stamp ``last_run`` and clear the failure record."""
    history_db.heartbeat_mark_success(
        action.id, now.isoformat(), _meta_for(action, **extra)
    )


def mark_failure(
    action: HeartbeatAction, now: datetime, reason: str, **extra: Any
) -> int:
    """Increment the failure counter (``last_run`` untouched). Returns the new count."""
    return history_db.heartbeat_mark_failure(
        action.id,
        now.isoformat(),
        _meta_for(action, last_outcome="failed", last_error=reason[:500], **extra),
    )


def forget_state(action_id: str) -> None:
    """Drop the state row (cancel / archive-eviction) so a reused id starts clean."""
    history_db.heartbeat_state_forget(action_id)


def prune_state(keep: set[str]) -> int:
    """Remove rows whose action is neither active nor in ``completed``."""
    removed = history_db.heartbeat_state_prune(keep)
    if removed:
        log.info("heartbeat state pruned %d orphan row(s)", removed)
    return removed


# One-time migration of the pre-SQLite ``heartbeat_state.json``. Accepts the
# v2 (``last_run`` + ``failures``), v1 (``last_run`` only) and legacy flat
# ``{"<id>": "<iso>"}`` layouts; the file is renamed to ``.bak`` afterwards.
_LEGACY_STATE_FILENAME = "heartbeat_state.json"
_legacy_state_checked = False
_legacy_state_lock = threading.Lock()


def _legacy_state_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(xdg) / "metalclaw" / _LEGACY_STATE_FILENAME


def _parse_legacy_state(raw: Any) -> list[history_db.HeartbeatStateRow]:
    if not isinstance(raw, dict):
        return []
    if "last_run" not in raw and "failures" not in raw and "version" not in raw:
        last_run_raw: Any = raw
        failures_raw: Any = {}
    else:
        last_run_raw = raw.get("last_run") or {}
        failures_raw = raw.get("failures") or {}
    rows: dict[str, list[Any]] = {}
    migrated_at = _now_iso()
    meta = json.dumps({"migrated_from": _LEGACY_STATE_FILENAME})
    if isinstance(last_run_raw, dict):
        for k, v in last_run_raw.items():
            rows[str(k)] = [str(k), str(v), 0, None, migrated_at, meta]
    if isinstance(failures_raw, dict):
        for k, v in failures_raw.items():
            if not isinstance(v, dict):
                continue
            try:
                count = int(v["count"])
                last_failed = str(v["last_failed"])
                parse_iso(last_failed)
            except KeyError, TypeError, ValueError:
                continue
            if count <= 0:
                continue
            row = rows.setdefault(str(k), [str(k), None, 0, None, migrated_at, meta])
            row[2] = count
            row[3] = last_failed
    return [tuple(r) for r in rows.values()]  # type: ignore[misc]


def _migrate_legacy_state_file() -> None:
    global _legacy_state_checked
    if _legacy_state_checked:
        return
    with _legacy_state_lock:
        if _legacy_state_checked:
            return
        _legacy_state_checked = True
        path = _legacy_state_path()
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("legacy heartbeat state unreadable, leaving in place: %s", e)
            return
        rows = _parse_legacy_state(raw)
        n = history_db.heartbeat_state_import(rows) if rows else 0
        backup = path.with_suffix(path.suffix + ".bak")
        try:
            os.replace(path, backup)
        except OSError as e:
            log.warning("could not rename %s -> %s: %s", path, backup, e)
        log.info(
            "migrated %d heartbeat state row(s) from %s into history.db (%s)",
            n,
            path.name,
            backup.name,
        )


def reset_state_migration_flag() -> None:
    """Test hook: force the legacy-file check to run again."""
    global _legacy_state_checked
    _legacy_state_checked = False


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


# --- Tick logic ---


def _build_action_session(
    action: HeartbeatAction, now: datetime, build_system_prompt
) -> chat_loop.Session:
    import chat_loop  # local: chat_loop imports heartbeat at module load

    system = build_system_prompt(format_now(now))
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
    session = chat_loop.Session(system=system)
    session.append_user("\n".join(user_lines))
    return session


def _resolve_channel(
    action_channel: str | None,
) -> tuple[str | None, channels.Channel | None]:
    cfg = get_config()
    target = action_channel or cfg.heartbeat_default_channel
    if not target:
        return None, None
    return target, channels.for_scope(target)


async def _run_action(
    action: HeartbeatAction, now: datetime, chat_fn, build_system_prompt
) -> str:
    session = _build_action_session(action, now, build_system_prompt)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: chat_fn(session))


# Single-flight guard: the scheduler loop and ``/heartbeat run`` both call
# ``run_tick``; overlapping ticks would see the same actions as due and run the
# model (and alert) twice. A plain flag rather than ``asyncio.Lock`` so it is
# loop-agnostic (tests spin up many loops) — asyncio is single-threaded, so
# check-and-set between awaits is race-free.
_tick_running = False
# Strong refs to fire-and-forget tick tasks: the event loop only holds weak
# refs, so an unreferenced ``create_task`` can be garbage-collected mid-run.
_background_ticks: set[asyncio.Task[dict[str, str]]] = set()


def is_tick_running() -> bool:
    return _tick_running


def spawn_tick() -> asyncio.Task[dict[str, str]] | None:
    """Start a tick in the background (``/heartbeat run``). Returns None if one
    is already running. The task is kept referenced until it finishes."""
    if _tick_running:
        log.info("heartbeat: manual tick requested but one is already running")
        return None
    task = asyncio.create_task(run_tick(), name="heartbeat-manual-tick")
    _background_ticks.add(task)
    task.add_done_callback(_background_ticks.discard)
    return task


async def run_tick(*, now: datetime | None = None) -> dict[str, str]:
    """Single heartbeat tick across all due actions. Returns ``{action_id: reply}``.

    A reply of ``""`` indicates ``HEARTBEAT_OK``-suppression. A successful
    one-shot (``kind=at``) action is moved to ``completed`` only after the
    notify call succeeds — failures keep the action active. If another tick
    is already in flight the call is skipped and returns ``{}``.
    """
    global _tick_running
    if _tick_running:
        log.info("heartbeat tick skipped: another tick is still running")
        return {}
    _tick_running = True
    try:
        return await _run_tick_locked(now=now)
    finally:
        _tick_running = False


async def _run_tick_locked(*, now: datetime | None = None) -> dict[str, str]:
    import chat_loop  # local to avoid circular import at module load

    now = now or datetime.now(UTC)
    cfg = get_config()
    tick_t0 = time.monotonic()
    if not _within_active_hours(now.astimezone(), cfg.heartbeat_active_hours):
        log.info(
            "heartbeat tick skipped: outside active hours %s (local hour=%d)",
            cfg.heartbeat_active_hours,
            now.astimezone().hour,
        )
        return {}

    # Ledger/state calls take flock and hit SQLite: run them off the loop
    # thread so a lock held by another process cannot stall the frontends.
    state = await asyncio.to_thread(load_state)
    snapshot = await asyncio.to_thread(load_ledger)
    replies: dict[str, str] = {}
    n_due = n_ran = n_suppressed = n_notified = n_failed = n_backoff = 0
    log.info("heartbeat tick start: %d active action(s)", len(snapshot.actions))

    async def _fail(action: HeartbeatAction, why: str, **extra: Any) -> None:
        nonlocal n_failed
        n_failed += 1
        count = await asyncio.to_thread(mark_failure, action, now, why, **extra)
        log.warning(
            "heartbeat action %s: %s — keeping active (failure #%d, next retry in %ds)",
            action.id,
            why,
            count,
            backoff_seconds(count),
        )

    for action in list(snapshot.actions):
        st = state.get(action.id)
        last_run_iso = st.last_run if st else None
        try:
            due = is_action_due(action, last_run_iso, now)
        except Exception:
            log.exception("heartbeat action %s: due-check crashed", action.id)
            n_failed += 1
            continue
        retry_at = st.retry_at() if st else None
        if due and st is not None and retry_at is not None and now < retry_at:
            n_backoff += 1
            log.info(
                "heartbeat action %s due but in backoff: %d failure(s), retry at %s",
                action.id,
                st.failure_count,
                retry_at.isoformat(timespec="minutes"),
            )
            continue
        if not due:
            log.debug(
                "heartbeat action %s not due: kind=%s last_run=%s at=%s every=%s schedule=%s",
                action.id,
                action.kind,
                last_run_iso,
                action.at,
                action.every,
                action.schedule,
            )
            continue
        n_due += 1
        log.info(
            "heartbeat action %s due: kind=%s channel=%s",
            action.id,
            action.kind,
            action.channel,
        )
        action_t0 = time.monotonic()

        try:
            reply = await _run_action(
                action, now, chat_loop.chat, chat_loop.build_system_prompt
            )
        except Exception:
            log.exception("heartbeat action %s: chat turn failed", action.id)
            await _fail(
                action,
                "chat turn failed",
                run_seconds=round(time.monotonic() - action_t0, 2),
            )
            continue
        n_ran += 1
        log.info(
            "heartbeat action %s ran in %.2fs reply=%d chars",
            action.id,
            time.monotonic() - action_t0,
            len(reply),
        )
        log.debug("heartbeat action %s reply: %.500s", action.id, reply)

        clean = reply.strip()
        acked = SENTINEL in clean
        if not acked and not clean:
            # Empty reply with no sentinel: the model produced nothing (common
            # for small local models after a tool round). Treat as a failed
            # run so the action stays active and one-shots are not archived.
            await _fail(
                action,
                f"empty reply without {SENTINEL}",
                run_seconds=round(time.monotonic() - action_t0, 2),
            )
            continue
        if acked:
            clean = clean.replace(SENTINEL, "").strip()
            if len(clean) <= _ACK_MAX_CHARS:
                if clean:
                    log.info(
                        "heartbeat action %s: ack suppressed, dropped text: %r",
                        action.id,
                        clean,
                    )
                clean = ""
        if not clean:
            n_suppressed += 1
            log.info("heartbeat action %s: HEARTBEAT_OK (nothing to report)", action.id)
            replies[action.id] = ""
            await asyncio.to_thread(
                mark_success,
                action,
                now,
                last_outcome="suppressed",
                reply_chars=len(reply),
                run_seconds=round(time.monotonic() - action_t0, 2),
            )
            if action.kind == ActionKind.AT:
                # one-shots that ack with HEARTBEAT_OK are still "done"
                await asyncio.to_thread(_archive_with_lock, action.id, now)
            continue

        target_scope, channel = _resolve_channel(action.channel)
        log.info(
            "heartbeat action %s: notify target=%r (action.channel=%r default=%r) channel=%s",
            action.id,
            target_scope,
            action.channel,
            cfg.heartbeat_default_channel,
            channel.name if channel else None,
        )
        if channel is None:
            await _fail(action, f"no channel resolves for {target_scope!r}")
            continue

        try:
            await channel.notify(target_scope or "", clean)
        except Exception as e:
            log.exception("heartbeat action %s: channel notify failed", action.id)
            await _fail(action, f"channel notify failed: {e}", target=target_scope)
            continue

        n_notified += 1
        log.info(
            "heartbeat action %s: notified %s (%d chars)",
            action.id,
            target_scope,
            len(clean),
        )
        replies[action.id] = clean
        await asyncio.to_thread(
            mark_success,
            action,
            now,
            last_outcome="notified",
            target=target_scope,
            reply_chars=len(clean),
            run_seconds=round(time.monotonic() - action_t0, 2),
        )
        if action.kind == ActionKind.AT:
            await asyncio.to_thread(_archive_with_lock, action.id, now)

    # Orphan rows (cancelled, or archived then evicted past _COMPLETED_CAP)
    # would otherwise make a reused id look "already run" forever.
    try:
        ledger_now = await asyncio.to_thread(load_ledger)
        keep = {a.id for a in ledger_now.actions}
        keep.update(str(c.get("id", "")) for c in ledger_now.completed)
        await asyncio.to_thread(prune_state, keep)
    except Exception:
        log.exception("heartbeat state prune failed")
    log.info(
        "heartbeat tick end: total=%d due=%d ran=%d suppressed=%d notified=%d failed=%d backoff=%d duration=%.2fs",
        len(snapshot.actions),
        n_due,
        n_ran,
        n_suppressed,
        n_notified,
        n_failed,
        n_backoff,
        time.monotonic() - tick_t0,
    )
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
                log.info("heartbeat action %s archived to completed", action_id)
                return


# --- System-prompt summary ---


def summary(max_chars: int = 400) -> str:
    """Compact summary of active actions for system-prompt injection."""
    try:
        actions = list_active()
    except Exception:  # noqa: BLE001
        return ""
    if not actions:
        return ""
    parts: list[str] = []
    for a in actions:
        if a.kind == ActionKind.AT:
            tail = f"at {a.at}"
        elif a.kind == ActionKind.CRON and a.schedule is not None:
            tail = (
                f"{','.join(a.schedule.days)} {a.schedule.time} {a.schedule.timezone}"
            )
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
        except Exception:
            log.exception("heartbeat tick crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
    log.info("heartbeat loop stopped")
