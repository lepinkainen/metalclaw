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
import fcntl
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

import channels
import history as history_db
from config import get_config
from heartbeat_schedule import (  # noqa: F401  re-exported for existing callers
    _INTERVAL_RE,
    _TIME_RE,
    _WEEKDAYS,
    ActionKind,
    CronSchedule,
    HeartbeatAction,
    _within_active_hours,
    is_action_due,
    next_due,
    normalise_weekdays,
    parse_interval,
    parse_iso,
    validate_time_string,
    validate_timezone,
)

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
_COMPLETED_MAX_AGE = timedelta(days=7)

_LEDGER_PROCESS_LOCK = threading.Lock()


@dataclass
class HeartbeatLedger:
    actions: list[HeartbeatAction] = field(default_factory=list)
    completed: list[dict[str, Any]] = field(default_factory=list)


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
        pipeline=str(raw["pipeline"]) if raw.get("pipeline") else None,
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
                history_db.heartbeat_merge_meta(
                    action_id, _meta_for(removed, cancelled_at=_now_iso())
                )
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
        "prompt": action.prompt,
    }
    if action.pipeline:
        meta["pipeline"] = action.pipeline
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


# --- Tick logic ---


def strip_sentinel(reply: str) -> tuple[str, bool]:
    """``(text, acked)``: ``acked`` when ``HEARTBEAT_OK`` appears. Short text
    around the sentinel (a "nothing to report" sentence) is dropped; a longer
    remainder is a real alert that happens to carry a stray sentinel."""
    clean = reply.strip()
    acked = SENTINEL in clean
    if acked:
        rest = clean.replace(SENTINEL, "").strip()
        if len(rest) <= _ACK_MAX_CHARS:
            if rest:
                log.info("heartbeat: ack suppressed, dropped text: %r", rest)
            rest = ""
        clean = rest
    return clean, acked


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


def config_pipeline_actions() -> list[HeartbeatAction]:
    """Pipeline actions declared under ``heartbeat_pipelines`` in config.yaml.

    They are not written to heartbeat.yaml (config is their source of truth);
    their state rows use the id ``pipeline-<name>`` unless ``id`` is set.
    """
    out: list[HeartbeatAction] = []
    for spec in get_config().heartbeat_pipelines:
        try:
            if spec.every is not None:
                kind, every, schedule = (
                    ActionKind.EVERY,
                    parse_interval(spec.every),
                    None,
                )
            elif spec.schedule:
                sd = spec.schedule
                kind, every = ActionKind.CRON, None
                schedule = CronSchedule(
                    days=normalise_weekdays(sd.get("days") or []),
                    time=validate_time_string(str(sd.get("time", ""))),
                    timezone=validate_timezone(str(sd.get("timezone", ""))),
                )
            else:
                raise ValueError("needs 'every' or 'schedule'")
        except ValueError as e:
            log.warning("heartbeat_pipelines: skipping %r: %s", spec.pipeline, e)
            continue
        out.append(
            HeartbeatAction(
                id=spec.id or f"pipeline-{spec.pipeline}",
                kind=kind,
                prompt="",
                channel=spec.channel or "",
                created_at="",
                created_from="config",
                schedule=schedule,
                every=every,
                pipeline=spec.pipeline,
            )
        )
    return out


async def _run_pipeline_action(action: HeartbeatAction, now: datetime) -> str:
    """Run ``action.pipeline`` and return its outcome. Delivery happens inside
    the pipeline; the trace row is written whatever happens. Raises on failure
    so the caller applies the usual ``mark_failure`` backoff."""
    import pipelines

    assert action.pipeline is not None
    fn = pipelines.REGISTRY.get(action.pipeline)
    if fn is None:
        raise ValueError(f"unknown pipeline {action.pipeline!r}")
    target_scope, channel = _resolve_channel(action.channel)

    async def _deliver(text: str) -> None:
        if channel is None:
            raise RuntimeError(f"no channel resolves for {target_scope!r}")
        await channel.notify(target_scope or "", text)
        log.info(
            "heartbeat pipeline %s: notified %s (%d chars)",
            action.id,
            target_scope,
            len(text),
        )

    trace = pipelines.Trace()
    ctx = pipelines.PipelineContext(
        config=get_config(),
        now=now,
        channel=target_scope,
        trace=trace,
        ledger=pipelines.Ledger(),
        ask_model=pipelines.context.default_ask_model(format_now(now)),
        deliver=_deliver,
    )
    run_id = secrets.token_hex(8)
    started = datetime.now(UTC)
    outcome: str = "failed"
    error: str | None = None
    try:
        outcome = str(await fn(ctx))
        return outcome
    except asyncio.CancelledError:
        # ``wait_for`` timeout in ``_run_tick_locked`` cancels us; record why.
        error = "cancelled (timeout)"
        raise
    except Exception as e:
        error = f"{type(e).__name__}: {e}"[:500]
        raise
    finally:
        try:
            await asyncio.to_thread(
                history_db.pipeline_run_record,
                run_id,
                action.pipeline,
                action.id,
                started.isoformat(),
                _now_iso(),
                outcome,
                error,
                trace.as_rows(),
            )
        except Exception:
            log.exception("heartbeat pipeline %s: trace write failed", action.id)
        log.info(
            "heartbeat pipeline %s run=%s outcome=%s steps=%s",
            action.id,
            run_id,
            outcome,
            "; ".join(
                f"{r['name']} {r['ms']}ms" + (f" {r['note']}" if r.get("note") else "")
                for r in trace.as_rows()
            ),
        )


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
    pipeline_actions = config_pipeline_actions()
    all_actions = list(snapshot.actions) + pipeline_actions
    replies: dict[str, str] = {}
    n_due = n_ran = n_suppressed = n_notified = n_failed = n_backoff = 0
    log.info(
        "heartbeat tick start: %d active action(s), %d pipeline(s)",
        len(snapshot.actions),
        len(pipeline_actions),
    )

    timeout = cfg.heartbeat_action_timeout_seconds

    async def _bounded[T](aw: Awaitable[T]) -> T:
        # NOTE: model actions run in an executor thread, which cannot be
        # cancelled: after a timeout the turn keeps running in the background
        # until it finishes and its result is discarded. The session is
        # scope-less so nothing is persisted, but its tool calls still execute.
        if timeout > 0:
            return await asyncio.wait_for(aw, timeout=timeout)
        return await aw

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

    for action in all_actions:
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

        if action.pipeline:
            try:
                outcome = await _bounded(_run_pipeline_action(action, now))
            except TimeoutError:
                await _fail(
                    action,
                    f"timed out after {timeout}s",
                    run_seconds=round(time.monotonic() - action_t0, 2),
                )
                continue
            except Exception as e:
                log.exception("heartbeat pipeline %s failed", action.id)
                await _fail(
                    action,
                    f"pipeline failed: {e}",
                    run_seconds=round(time.monotonic() - action_t0, 2),
                )
                continue
            n_ran += 1
            if outcome == "delivered":
                n_notified += 1
            else:
                n_suppressed += 1
            replies[action.id] = outcome if outcome == "delivered" else ""
            await asyncio.to_thread(
                mark_success,
                action,
                now,
                last_outcome=outcome,
                run_seconds=round(time.monotonic() - action_t0, 2),
            )
            continue

        try:
            reply = await _bounded(
                _run_action(action, now, chat_loop.chat, chat_loop.build_system_prompt)
            )
        except TimeoutError:
            await _fail(
                action,
                f"timed out after {timeout}s",
                run_seconds=round(time.monotonic() - action_t0, 2),
            )
            continue
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

        clean, acked = strip_sentinel(reply)
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

    try:
        await asyncio.to_thread(_expire_completed_with_lock, now)
    except Exception:
        log.exception("heartbeat completed expiry failed")
    log.info(
        "heartbeat tick end: total=%d due=%d ran=%d suppressed=%d notified=%d failed=%d backoff=%d duration=%.2fs",
        len(all_actions),
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
                history_db.heartbeat_merge_meta(
                    action_id, _meta_for(removed, completed_at=completed_at)
                )
                log.info("heartbeat action %s archived to completed", action_id)
                return


def _expire_completed_with_lock(now: datetime) -> int:
    cutoff = now - _COMPLETED_MAX_AGE
    with _ledger_lock():
        ledger = _read_ledger_locked()
        keep: list[dict] = []
        dropped: list[str] = []
        for c in ledger.completed:
            try:
                old = parse_iso(str(c["completed_at"])) < cutoff
            except KeyError, ValueError:
                old = False
            if old:
                dropped.append(str(c.get("id", "")))
            else:
                keep.append(c)
        if dropped:
            ledger.completed = keep
            _write_ledger_locked(ledger)
            log.info("heartbeat expired completed actions: %s", ", ".join(dropped))
        return len(dropped)


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
        what = f"pipeline:{a.pipeline}" if a.pipeline else a.prompt[:80]
        parts.append(f"{a.id} [{a.kind}] → {a.channel} | {tail} | {what}")
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
