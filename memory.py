"""User long-term memory stored as Obsidian-flavoured markdown.

Single-user bot — one file at <vault>/<memory_subdir>/memory.md. Sections:
  ## Preferences  — `- **key**: value` entries
  ## Facts        — free-form bullets
  ## Instructions — free-form bullets
"""

from __future__ import annotations

import contextlib
import enum
import fcntl
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import get_config

log = logging.getLogger("metalclaw.memory")

_FILENAME = "memory.md"
_SECTIONS = ("Preferences", "Facts", "Instructions")
_PREF_LINE = re.compile(r"^-\s+\*\*(?P<key>[^*]+?)\*\*:\s*(?P<value>.*)$")
_BULLET_LINE = re.compile(r"^-\s+(.+)$")

_PROCESS_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: "tuple[int, Memory] | None" = None


class ForgetStatus(enum.StrEnum):
    REMOVED = "removed"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class _CandidateKind(enum.StrEnum):
    PREF = "pref"
    FACT = "fact"
    INSTRUCTION = "instruction"


@dataclass
class Memory:
    preferences: dict[str, str] = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    instructions: list[str] = field(default_factory=list)
    updated: str | None = None


@dataclass
class ForgetResult:
    """Result of an attempted ``forget()``.

    On :attr:`ForgetStatus.REMOVED`, ``entry`` holds the deleted entry's
    display string (``"[pref] **key**: value"`` / ``"[fact] text"`` /
    ``"[instruction] text"``). On :attr:`ForgetStatus.AMBIGUOUS`, ``matches``
    lists every candidate in the same display format so the caller can refine
    the matcher.
    """
    status: ForgetStatus
    entry: str | None = None
    matches: list[str] = field(default_factory=list)


def _path() -> Path:
    cfg = get_config()
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    return cfg.memory_dir / _FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(text: str) -> Memory:
    mem = Memory()
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
        f"updated: {mem.updated or _now_iso()}",
        "---",
        "",
        "# Metalclaw Memory",
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


def _read_locked() -> Memory:
    path = _path()
    if not path.exists():
        return Memory()
    return _parse(path.read_text(encoding="utf-8"))


def _invalidate_cache() -> None:
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None


def _write_locked(mem: Memory) -> None:
    """Atomic write: write to temp file in same dir, then os.replace."""
    mem.updated = _now_iso()
    path = _path()
    text = _render(mem)
    fd, tmp = tempfile.mkstemp(prefix=".memory-", suffix=".md", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _invalidate_cache()


@contextlib.contextmanager
def _locked():
    """Process + cross-process exclusive lock on a sidecar lockfile."""
    cfg = get_config()
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    lock_path = cfg.memory_dir / f"{_FILENAME}.lock"
    with _PROCESS_LOCK:
        with open(lock_path, "w") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _copy(mem: Memory) -> Memory:
    return Memory(
        preferences=dict(mem.preferences),
        facts=list(mem.facts),
        instructions=list(mem.instructions),
        updated=mem.updated,
    )


def load(*, copy: bool = True) -> Memory:
    """Read memory.

    Backed by an mtime-keyed cache so per-turn ``summary()`` calls don't re-parse.
    Defaults to returning a defensive copy so callers can mutate without
    polluting the cache. Read-only callers (e.g. ``summary()``) can pass
    ``copy=False`` to skip the dict/list duplication on the hot path.
    """
    global _CACHE
    path = _path()
    if not path.exists():
        return Memory()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        with _locked():
            return _read_locked() if not copy else _copy(_read_locked())
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE[0] == mtime:
            return _copy(_CACHE[1]) if copy else _CACHE[1]
    with _locked():
        mem = _read_locked()
        try:
            fresh_mtime = path.stat().st_mtime_ns
        except OSError:
            fresh_mtime = mtime
    with _CACHE_LOCK:
        _CACHE = (fresh_mtime, mem)
    return _copy(mem) if copy else mem


def render_full() -> str:
    with _locked():
        path = _path()
        if not path.exists():
            return _render(Memory())
        return path.read_text(encoding="utf-8")


def _mutate(
    mutator: Callable[[Memory], None],
    log_fmt: str,
    *log_args: object,
) -> None:
    """Read-modify-write under the file lock, then log lazily via stdlib formatting."""
    with _locked():
        mem = _read_locked()
        mutator(mem)
        _write_locked(mem)
    log.info(log_fmt, *log_args)


def set_preference(key: str, value: str) -> None:
    def apply(mem: Memory) -> None:
        mem.preferences[key] = value
    _mutate(apply, "memory write op=set_preference key=%s", key)


def add_fact(text: str) -> None:
    def apply(mem: Memory) -> None:
        if text not in mem.facts:
            mem.facts.append(text)
    _mutate(apply, "memory write op=add_fact text=%r", text[:60])


def add_instruction(text: str) -> None:
    def apply(mem: Memory) -> None:
        if text not in mem.instructions:
            mem.instructions.append(text)
    _mutate(apply, "memory write op=add_instruction text=%r", text[:60])


def forget(matcher: str) -> ForgetResult:
    """Delete a memory entry by case-insensitive substring match.

    Forget is a final operation — never silently picks one of multiple
    matches. Returns :class:`ForgetResult` with status REMOVED / AMBIGUOUS /
    NOT_FOUND.
    """
    needle = matcher.lower()
    with _locked():
        mem = _read_locked()

        candidates: list[tuple[_CandidateKind, str, str]] = []
        for k, v in mem.preferences.items():
            if needle in k.lower() or needle in v.lower():
                candidates.append((_CandidateKind.PREF, k, f"**{k}**: {v}"))
        for f in mem.facts:
            if needle in f.lower():
                candidates.append((_CandidateKind.FACT, f, f))
        for inst in mem.instructions:
            if needle in inst.lower():
                candidates.append((_CandidateKind.INSTRUCTION, inst, inst))

        if not candidates:
            log.info("memory write op=forget status=not_found matcher=%r", matcher)
            return ForgetResult(status=ForgetStatus.NOT_FOUND)
        if len(candidates) > 1:
            display = [f"[{kind}] {disp}" for kind, _, disp in candidates]
            log.info(
                "memory write op=forget status=ambiguous matcher=%r match_count=%d",
                matcher, len(candidates),
            )
            return ForgetResult(status=ForgetStatus.AMBIGUOUS, matches=display)

        kind, ident, disp = candidates[0]
        if kind is _CandidateKind.PREF:
            del mem.preferences[ident]
        elif kind is _CandidateKind.FACT:
            mem.facts.remove(ident)
        else:
            mem.instructions.remove(ident)
        _write_locked(mem)
    entry = f"[{kind}] {disp}"
    log.info("memory write op=forget status=removed entry=%r", entry)
    return ForgetResult(status=ForgetStatus.REMOVED, entry=entry)


def migrate_legacy_scopes() -> list[str]:
    """One-shot migration of pre-collapse per-scope memory files into ``memory.md``.

    Looks for ``cli.md``, ``telegram-*.md``, ``discord-*.md`` siblings of the
    current memory file. Parses each (tolerant of the old ``scope:`` frontmatter
    line), unions preferences (last-writer-wins on key collisions), unions
    facts and instructions (deduped), writes the merged result to
    ``memory.md``, and renames each source to ``<name>.bak``.

    Idempotent: returns an empty list if no legacy files are present.
    """
    cfg = get_config()
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    legacy: list[Path] = [cfg.memory_dir / "cli.md"]
    legacy.extend(cfg.memory_dir.glob("telegram-*.md"))
    legacy.extend(cfg.memory_dir.glob("discord-*.md"))
    legacy = [p for p in legacy if p.exists()]
    if not legacy:
        return []

    with _locked():
        merged = _read_locked()
        for src in legacy:
            mem = _parse(src.read_text(encoding="utf-8"))
            for k, v in mem.preferences.items():
                merged.preferences[k] = v
            for f in mem.facts:
                if f not in merged.facts:
                    merged.facts.append(f)
            for inst in mem.instructions:
                if inst not in merged.instructions:
                    merged.instructions.append(inst)
        _write_locked(merged)
        migrated: list[str] = []
        for src in legacy:
            bak = src.with_suffix(src.suffix + ".bak")
            src.rename(bak)
            migrated.append(src.name)
        return migrated


def summary(max_chars: int = 600) -> str:
    """Compact one-block summary for system-prompt injection. Empty string if nothing stored."""
    mem = load(copy=False)
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
        hint = " (call get_user_memory for full memory)"
        text = text[: max(0, max_chars - len(hint) - 1)].rstrip() + "…" + hint
    return text
