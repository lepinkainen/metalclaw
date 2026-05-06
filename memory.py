"""Per-scope user memory stored as Obsidian-flavoured markdown.

One file per scope at <vault>/<memory_subdir>/<scope>.md. Sections:
  ## Preferences  — `- **key**: value` entries
  ## Facts        — free-form bullets
  ## Instructions — free-form bullets

Scope is selected via the `current_scope` ContextVar so tools don't need a scope
parameter exposed to the model.
"""

from __future__ import annotations

import re
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import get_config

current_scope: ContextVar[str] = ContextVar("current_scope", default="cli")

_SECTIONS = ("Preferences", "Facts", "Instructions")
_PREF_LINE = re.compile(r"^-\s+\*\*(?P<key>[^*]+?)\*\*:\s*(?P<value>.*)$")
_BULLET_LINE = re.compile(r"^-\s+(.+)$")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(scope: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(scope)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[scope] = lock
        return lock


@dataclass
class Memory:
    scope: str
    preferences: dict[str, str] = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    updated: str | None = None


def _path(scope: str) -> Path:
    cfg = get_config()
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    return cfg.memory_dir / f"{scope}.md"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(text: str, scope: str) -> Memory:
    mem = Memory(scope=scope)
    if not text.strip():
        return mem

    lines = text.splitlines()
    i = 0

    if lines and lines[0].strip() == "---":
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            line = lines[i]
            if line.startswith("updated:"):
                mem.updated = line.split(":", 1)[1].strip()
            i += 1
        if i < len(lines):
            i += 1

    section: str | None = None
    while i < len(lines):
        line = lines[i].rstrip()
        if line.startswith("## "):
            heading = line[3:].strip()
            section = heading if heading in _SECTIONS else None
        elif section == "Preferences":
            m = _PREF_LINE.match(line)
            if m:
                mem.preferences[m.group("key").strip()] = m.group("value").strip()
        elif section in ("Facts", "Instructions"):
            m = _BULLET_LINE.match(line)
            if m:
                target = mem.facts if section == "Facts" else mem.instructions
                target.append(m.group(1).strip())
        i += 1

    return mem


def _render(mem: Memory) -> str:
    lines = [
        "---",
        f"scope: {mem.scope}",
        f"updated: {mem.updated or _now_iso()}",
        "---",
        "",
        f"# Metalclaw Memory — {mem.scope}",
        "",
        "## Preferences",
    ]
    if mem.preferences:
        for k, v in mem.preferences.items():
            lines.append(f"- **{k}**: {v}")
    lines.append("")

    lines.append("## Facts")
    for f in mem.facts:
        lines.append(f"- {f}")
    lines.append("")

    lines.append("## Instructions")
    for inst in mem.instructions:
        lines.append(f"- {inst}")
    lines.append("")

    return "\n".join(lines)


def _read(scope: str) -> Memory:
    path = _path(scope)
    if not path.exists():
        return Memory(scope=scope)
    return _parse(path.read_text(encoding="utf-8"), scope)


def _write(mem: Memory) -> None:
    mem.updated = _now_iso()
    _path(mem.scope).write_text(_render(mem), encoding="utf-8")


def load(scope: str | None = None) -> Memory:
    scope = scope or current_scope.get()
    with _lock_for(scope):
        return _read(scope)


def render_full(scope: str | None = None) -> str:
    scope = scope or current_scope.get()
    with _lock_for(scope):
        path = _path(scope)
        if not path.exists():
            return _render(Memory(scope=scope))
        return path.read_text(encoding="utf-8")


def set_preference(key: str, value: str, scope: str | None = None) -> None:
    scope = scope or current_scope.get()
    with _lock_for(scope):
        mem = _read(scope)
        mem.preferences[key] = value
        _write(mem)


def add_fact(text: str, scope: str | None = None) -> None:
    scope = scope or current_scope.get()
    with _lock_for(scope):
        mem = _read(scope)
        if text not in mem.facts:
            mem.facts.append(text)
        _write(mem)


def add_instruction(text: str, scope: str | None = None) -> None:
    scope = scope or current_scope.get()
    with _lock_for(scope):
        mem = _read(scope)
        if text not in mem.instructions:
            mem.instructions.append(text)
        _write(mem)


def forget(matcher: str, scope: str | None = None) -> bool:
    """Remove first entry containing `matcher` (case-insensitive). Returns True if removed."""
    scope = scope or current_scope.get()
    needle = matcher.lower()
    with _lock_for(scope):
        mem = _read(scope)

        for k, v in list(mem.preferences.items()):
            if needle in k.lower() or needle in v.lower():
                del mem.preferences[k]
                _write(mem)
                return True

        for bucket in (mem.facts, mem.instructions):
            for i, entry in enumerate(bucket):
                if needle in entry.lower():
                    bucket.pop(i)
                    _write(mem)
                    return True

        return False


def summary(scope: str | None = None, max_chars: int = 600) -> str:
    """Compact one-block summary for system-prompt injection. Empty string if nothing stored."""
    scope = scope or current_scope.get()
    mem = load(scope)
    if not (mem.preferences or mem.facts or mem.instructions):
        return ""

    parts: list[str] = []
    if mem.preferences:
        prefs = "; ".join(f"{k}={v}" for k, v in mem.preferences.items())
        parts.append(f"preferences: {prefs}")
    if mem.facts:
        parts.append("facts: " + " | ".join(mem.facts))
    if mem.instructions:
        parts.append("instructions: " + " | ".join(mem.instructions))

    text = "\n".join(parts)
    if len(text) > max_chars:
        text = text[: max_chars - 1].rstrip() + "…"
    return text
