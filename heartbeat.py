"""Heartbeat scheduler.

Periodic asyncio loop that wakes Metalclaw on a per-scope `HEARTBEAT.md`,
filters tasks to those due, calls `bot.chat()` with a synthetic prompt, and
routes any non-`HEARTBEAT_OK` reply to the matching frontend channel.

State (per-scope, per-task last-run timestamps) is persisted at
`$XDG_DATA_HOME/metalclaw/heartbeat_state.json`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

import channels
import memory
from config import get_config

log = logging.getLogger("metalclaw.heartbeat")

SENTINEL = "HEARTBEAT_OK"
_STATE_VERSION = 1
_INTERVAL_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)


# --- HEARTBEAT.md model ---


@dataclass
class HeartbeatTask:
    name: str
    interval_seconds: int
    prompt: str
    precheck: str | None = None


@dataclass
class HeartbeatFile:
    tasks: list[HeartbeatTask] = field(default_factory=list)
    body: str = ""

    def is_empty(self) -> bool:
        return not self.tasks and not self.body.strip()


def parse_interval(text: str | int) -> int:
    if isinstance(text, int):
        return int(text)
    s = str(text).strip()
    if s.isdigit():
        return int(s)
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"invalid interval: {text!r}")
    n = int(m.group(1))
    unit = m.group(2).lower()
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def parse_heartbeat_file(text: str) -> HeartbeatFile:
    """Extract `tasks:` block + free-form body.

    Accepted layouts:
      1. `---`-delimited frontmatter (YAML) followed by markdown body.
      2. A fenced ```yaml ... ``` block followed by markdown body.
      3. A bare YAML document (the whole file). Body is empty.
      4. Pure markdown (no YAML). No tasks; body is the whole text.
    """
    yaml_block, body = _split_yaml(text)
    tasks: list[HeartbeatTask] = []
    if yaml_block is not None and yaml_block.strip():
        try:
            data = yaml.safe_load(yaml_block) or {}
        except yaml.YAMLError as e:
            raise ValueError(f"HEARTBEAT.md YAML parse error: {e}") from e
        if not isinstance(data, dict):
            raise ValueError("HEARTBEAT.md YAML block must be a mapping")
        raw_tasks = data.get("tasks") or []
        if not isinstance(raw_tasks, list):
            raise ValueError("HEARTBEAT.md `tasks:` must be a list")
        for entry in raw_tasks:
            if not isinstance(entry, dict):
                raise ValueError(f"task entry must be a mapping, got {type(entry).__name__}")
            name = entry.get("name")
            interval = entry.get("interval")
            prompt = entry.get("prompt", "")
            if not name or interval is None:
                raise ValueError(f"task missing name/interval: {entry!r}")
            tasks.append(
                HeartbeatTask(
                    name=str(name),
                    interval_seconds=parse_interval(interval),
                    prompt=str(prompt),
                    precheck=entry.get("precheck"),
                )
            )
    return HeartbeatFile(tasks=tasks, body=body.strip())


def _split_yaml(text: str) -> tuple[str | None, str]:
    stripped = text.lstrip("﻿")
    s = stripped.lstrip()
    if s.startswith("---\n") or s.startswith("---\r\n"):
        rest = s.split("\n", 1)[1] if "\n" in s else ""
        end = rest.find("\n---")
        if end == -1:
            return None, text
        block = rest[:end]
        body = rest[end + 4 :]
        return block, body.lstrip("\n")
    lower = s[:8].lower()
    if lower.startswith("```yaml"):
        rest = s.split("\n", 1)[1] if "\n" in s else ""
        end = rest.find("```")
        if end == -1:
            return None, text
        block = rest[:end]
        body = rest[end + 3 :]
        return block, body.lstrip("\n")
    try:
        loaded = yaml.safe_load(stripped)
    except yaml.YAMLError:
        return None, text
    if isinstance(loaded, dict) and "tasks" in loaded:
        return stripped, ""
    return None, text


# --- State file ---


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


def state_key(scope: str, task_name: str) -> str:
    return f"{scope}::{task_name}"


def is_due(state: dict[str, str], scope: str, task: HeartbeatTask, now: datetime) -> bool:
    last_iso = state.get(state_key(scope, task.name))
    if not last_iso:
        return True
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() >= task.interval_seconds


# --- Scope discovery ---


def heartbeat_path_for(scope: str) -> Path:
    cfg = get_config()
    return cfg.memory_dir / f"heartbeat-{scope}.md"


def discover_scopes() -> list[str]:
    """Scopes with a heartbeat-<scope>.md file. Falls back to active channels."""
    cfg = get_config()
    scopes: set[str] = set()
    if cfg.memory_dir.exists():
        for p in cfg.memory_dir.glob("heartbeat-*.md"):
            scope = p.stem[len("heartbeat-") :]
            if scope:
                scopes.add(scope)
    return sorted(scopes)


# --- Tick logic ---


def _within_active_hours(now: datetime, window: tuple[int, int] | None) -> bool:
    if window is None:
        return True
    start, end = window
    h = now.hour
    if start <= end:
        return start <= h < end
    return h >= start or h < end


def _build_heartbeat_messages(scope: str, hb: HeartbeatFile, due: list[HeartbeatTask], now: datetime, build_system_prompt) -> list[dict]:
    local = now.astimezone() if now.tzinfo else now
    now_str = local.strftime("%Y-%m-%d %H:%M")
    system = build_system_prompt(scope, now_str)
    system += (
        "\n\nHEARTBEAT MODE: This is a scheduled wake-up, not a user message. "
        "Run only the tasks listed below. Use tools as needed. "
        f"If nothing requires the user's attention, reply with exactly `{SENTINEL}` "
        "and nothing else. Otherwise, reply with a concise alert the user should see."
    )

    lines = ["Heartbeat tick. Tasks due now:", ""]
    for t in due:
        lines.append(f"- **{t.name}**: {t.prompt}")
    if hb.body:
        lines.append("")
        lines.append("Additional context:")
        lines.append(hb.body)

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n".join(lines)},
    ]


async def run_tick(*, now: datetime | None = None) -> dict[str, str]:
    """Single heartbeat tick across all eligible scopes. Returns per-scope reply (or "" if silent)."""
    import bot  # local to avoid circular import at module load

    now = now or datetime.now(timezone.utc)
    cfg = get_config()
    if not _within_active_hours(now.astimezone(), cfg.heartbeat_active_hours):
        log.debug("heartbeat tick outside active hours")
        return {}

    state = load_state()
    replies: dict[str, str] = {}

    for scope in discover_scopes():
        path = heartbeat_path_for(scope)
        if not path.exists():
            continue
        try:
            hb = parse_heartbeat_file(path.read_text(encoding="utf-8"))
        except ValueError as e:
            log.warning("scope %s: bad HEARTBEAT.md: %s", scope, e)
            continue
        if hb.is_empty():
            continue

        due = [t for t in hb.tasks if is_due(state, scope, t, now)]
        if not due and not hb.body:
            continue

        try:
            reply = await _run_scope(scope, hb, due, now, bot.chat, bot.build_system_prompt)
        except Exception as e:
            log.exception("scope %s: heartbeat turn failed: %s", scope, e)
            continue

        for t in due:
            state[state_key(scope, t.name)] = now.isoformat()

        clean = reply.strip()
        if clean == SENTINEL or clean.startswith(SENTINEL):
            replies[scope] = ""
            continue
        replies[scope] = clean

        channel = channels.for_scope(scope)
        if channel is None:
            log.warning("scope %s: no channel registered, dropping reply", scope)
            continue
        try:
            await channel.notify(scope, clean)
        except Exception as e:
            log.exception("scope %s: channel notify failed: %s", scope, e)

    save_state(state)
    return replies


async def _run_scope(scope, hb, due, now, chat_fn, build_system_prompt) -> str:
    token = memory.current_scope.set(scope)
    try:
        messages = _build_heartbeat_messages(scope, hb, due, now, build_system_prompt)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: chat_fn(messages))
    finally:
        memory.current_scope.reset(token)


async def run(stop: asyncio.Event) -> None:
    """Main loop: tick, sleep interval, repeat until `stop` is set."""
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
