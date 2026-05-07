# memory

Single-user. One file. No scopes.

## Location

`<vault_path>/<memory_subdir>/memory.md` — both from `cfg`. Default `memory_subdir = "Metalclaw/Memory"`.

## File format

```markdown
---
updated: 2026-05-07T08:32:14+00:00
---

# Metalclaw Memory

## Preferences
- **key**: value
- **another_key**: value with [[wikilinks]]

## Facts
- Free-form bullet
- Another fact

## Instructions
- Always reply in Finnish unless the user writes in English.
- Use metric units for distances.
```

- Frontmatter: only `updated:` is parsed. ISO-8601 UTC.
- Sections in fixed order: `Preferences`, `Facts`, `Instructions` (`memory.py:29 _SECTIONS`).
- Preference line: `^-\s+\*\*(?P<key>[^*]+?)\*\*:\s*(?P<value>.*)$`.
- Bullet: `^-\s+(.+)$`.

## Locking

`_locked()` ctxmgr (`memory.py:179`):
1. `threading.Lock` — process-wide, exclusive.
2. `fcntl.flock(LOCK_EX)` on sidecar `memory.md.lock` — cross-process exclusive.

Both required because `--daemon` and an interactive `bot.py` may run simultaneously.

## Atomic write

`_write_locked(mem)` (`memory.py:162`):
1. `tempfile.mkstemp(prefix=".memory-", suffix=".md", dir=path.parent)` — same dir for atomic `os.replace`.
2. Write text to fd, close.
3. `os.replace(tmp, path)` — atomic on POSIX.
4. On exception: `os.unlink(tmp)`.
5. `_invalidate_cache()`.

## Cache

`_CACHE: tuple[mtime_ns, Memory] | None`. Hit if `path.stat().st_mtime_ns == _CACHE[0]`. Default `load(copy=True)` returns deep copy. `summary()` calls with `copy=False` to skip dict/list duplication on hot path.

Reads happen under `_locked()` only on cache miss — concurrent readers don't block each other when the file hasn't changed.

## Mutators

All go through `_mutate(fn, log_fmt, *args)` (`memory.py:242`):
- Acquire lock → read → mutate → write → release. Then `log.info(...)`.
- Logging is **outside** the lock to avoid log handler lock contention.

| Function | Semantics |
|---|---|
| `set_preference(k, v)` | Overwrites; idempotent. |
| `add_fact(t)` | Appends if not already present (exact string match). |
| `add_instruction(t)` | Appends if not already present. |
| `forget(matcher)` | See below. |

## `forget(matcher) -> ForgetResult`

- Case-insensitive substring match against keys, values, fact text, instruction text.
- 0 matches → `ForgetStatus.NOT_FOUND`.
- 1 match → `REMOVED`, `entry` = `[pref|fact|instruction] <display>`.
- ≥2 matches → `AMBIGUOUS`, `matches` = full candidate list. **Deletes nothing.** Caller must refine and retry.

## `summary(max_chars=600)`

Compact one-block format for system-prompt injection:
```
preferences: k1=v1; k2=v2
facts: f1 | f2 | f3
instructions: i1 | i2
```
If over `max_chars`, truncates to `max_chars - len(hint) - 1` and appends `… (call get_user_memory for full memory)` so the model knows to fetch more.

Empty memory → empty string → no injection.

## Migration

`migrate_legacy_scopes() -> list[str]` runs once at startup (`bot.py:67`). Looks for siblings `cli.md`, `telegram-*.md`, `discord-*.md`. Merges:
- Preferences: last-writer-wins on key collision.
- Facts/Instructions: union, dedup by exact string.

Result written to `memory.md`; sources renamed to `*.bak`. Idempotent — empty list once gone.

## Testing

`tests/test_memory.py` — covers parse/render round-trip, set/add/forget, ambiguous matcher, single-file memory, cross-process lock pattern.

## Don't

- Don't bypass `_locked()` for reads-with-mutation. Reads outside the lock are fine via `load()`.
- Don't return raw `_CACHE[1]` to mutating callers (always `_copy()` unless `copy=False` and caller is read-only).
- Don't rename sections — `_SECTIONS` is a tuple constant; many tests assume the three names.
