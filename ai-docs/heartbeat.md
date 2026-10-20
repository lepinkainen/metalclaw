# heartbeat

Proactive scheduler. Runs in `bot._async_main` as `asyncio.create_task(heartbeat.run(stop))`.

## Ledger: `<vault>/<memory_subdir>/heartbeat.yaml`

Bot-owned. Users do **not** edit it by hand — actions are created via the model-facing tools `create_heartbeat_action` / `list_heartbeat_actions` / `cancel_heartbeat_action` (see `tools/heartbeat_tools.py`).

```yaml
version: 1
actions:
  - id: ab12cd                        # 6-char hex slug, used by cancel
    kind: at                          # at | cron | every
    prompt: |
      Remind me to check the kettle.
    channel: cli                      # cli | telegram-<id> | discord-<id>
    created_at: 2026-05-08T10:00:00+00:00
    created_from: cli                 # active scope at creation, or null
    at: 2026-05-08T10:30:00+00:00     # kind=at only
  - id: ef34gh
    kind: cron
    prompt: Summarise overnight email
    channel: telegram-12345
    created_at: ...
    created_from: telegram-12345
    schedule:                         # kind=cron only
      days: [mon, thu]                # 3-letter abbrevs, lower-case
      time: "07:00"                   # HH:MM 24h
      timezone: Europe/Helsinki       # IANA
  - id: ij56kl
    kind: every
    prompt: Watch high-priority email
    channel: cli
    created_at: ...
    created_from: cli
    every: 1800                       # seconds (>0)
completed:
  - id: ...                           # snapshot of original action plus
    completed_at: ...                 # the completion timestamp
    ...                               # capped at 50 entries
```

I/O is atomic: tempfile + `os.replace` under a per-process `threading.Lock` plus `fcntl.flock` on `heartbeat.yaml.lock` (`heartbeat.py:_ledger_lock`).

## State: `heartbeat_state` table in `$XDG_DATA_HOME/metalclaw/history.db`

```sql
CREATE TABLE heartbeat_state (
    action_id     TEXT    PRIMARY KEY,
    last_run      TEXT,                      -- ISO-8601; NULL = never ran
    failure_count INTEGER NOT NULL DEFAULT 0, -- consecutive failures (backoff)
    last_failed   TEXT,                      -- ISO-8601 of the latest failure
    updated_at    TEXT    NOT NULL,          -- UTC ISO of the last write
    meta          TEXT    NOT NULL DEFAULT '{}' -- JSON, debugging only
);
```

Row-level SQL lives in `history.py` (`heartbeat_state_all/get`, `heartbeat_mark_success/failure`, `heartbeat_state_forget/prune/import`) — heartbeat-agnostic, shared locked connection. `heartbeat.py` wraps it: `ActionState` (dataclass view of one row, `retry_at()`), `load_state() -> dict[id, ActionState]`, `get_state(id)`, `mark_success(action, now, **meta)`, `mark_failure(action, now, reason, **meta) -> count`, `forget_state(id)`, `prune_state(keep)`.

Per-row upserts: no whole-file read-modify-write, so `--daemon` + interactive `bot.py`, or `/heartbeat run` racing the scheduler tick, cannot lose each other's writes. SQLite `RETURNING` (≥3.35) is used by `heartbeat_mark_failure`.

**`meta` column.** Free-form JSON merged (dict-update, not replaced) on every write via `_meta_for(action, **extra)`: always `kind`, `channel`, `prompt` (≤120 chars), plus `at` / `every` / `schedule`; successes add `last_outcome` (`notified` | `suppressed`), `target`, `reply_chars`, `run_seconds`; failures add `last_outcome: failed`, `last_error` (≤500 chars), `run_seconds` / `target` where known. Nothing reads `meta` at runtime — it exists so `sqlite3 history.db 'select * from heartbeat_state'` explains what the scheduler did after the fact. Add keys freely; never make the due logic depend on them. Unparseable `meta` is kept under `_corrupt_meta` rather than dropped.

**Lifecycle.** `cancel()` → `forget_state(id)`. End of every `run_tick` → `prune_state(active ∪ completed ids)` removes orphan rows (cancelled, or archived then evicted past `_COMPLETED_CAP`). `create_action` passes `load_state()` keys into `_generate_id` so an id with live state is never recycled (a reused `at` id would otherwise look "already run" forever).

**Legacy migration.** The pre-SQLite `heartbeat_state.json` (flat `{"<id>": "<iso>"}`, v1 `{"version":1,"last_run":{…}}`, v2 with `failures`) is imported once by `_migrate_legacy_state_file()` on the first `load_state()`/`get_state()` of the process (guarded by `_legacy_state_checked`), then renamed to `heartbeat_state.json.bak`. Rows get `meta = {"migrated_from": "heartbeat_state.json"}`. `reset_state_migration_flag()` is the test hook.

**Failure backoff.** Every failure path in `run_tick` (chat exception, empty reply without sentinel, no resolvable channel, `notify` raised) calls `mark_failure`; a success (notified or `HEARTBEAT_OK`) calls `mark_success` which stamps `last_run` and zeroes the counter. `backoff_seconds(count) = min(heartbeat_interval_seconds * 2**(count-1), heartbeat_failure_backoff_max_seconds)` (cap default 3600). `ActionState.retry_at()` = `last_failed + backoff`; a due action with `now < retry_at` is skipped **before** any model call (logged at INFO, counted as `backoff=` in the tick-end line). Net effect: first failure retries next tick, then every 2nd, 4th… tick, up to once per hour. `/status` shows `next: <retry time> (N failures, backing off)`.

## Due logic (`heartbeat.is_action_due`)

| kind | due iff |
|------|---------|
| `at` | `last_run_iso is None` AND `now >= parse_iso(at)` — `parse_iso` treats a tz-naive `at` as **local** time (matches the local wall clock the model sees in the prompt), aware values are honoured as given |
| `every` | first run OR `(now - last_run).total_seconds() >= every` |
| `cron` | local weekday in `schedule.days` AND `local_now >= scheduled_today` AND (no `last_run` OR `last_run < scheduled_today` in the same `tz`) |

`cron` uses `zoneinfo.ZoneInfo`. A missed schedule (server down at 07:00, tick at 09:00) still fires — better late than never — and updates `last_run` so it won't re-fire today.

`heartbeat.next_due(action, last_run_iso, now)` returns when an action is next due (`now` if due already, None if never again); used by `/status`. Actual execution waits for the next tick.

## `run_tick` (`heartbeat.py:run_tick`)

Single-flight: a module-level `_tick_running` flag (not an `asyncio.Lock`, so it works across the many event loops tests create) makes an overlapping call log + return `{}` instead of running the same due actions twice. `spawn_tick()` is the only sanctioned way to fire-and-forget a tick: it refuses while one runs and keeps the task in `_background_ticks` until done (the loop holds only weak refs; an unreferenced `create_task` can be GC'd mid-run). The body lives in `_run_tick_locked`.

1. Active-hours gate (`_within_active_hours(now.astimezone(), cfg.heartbeat_active_hours)`). Wraparound `[22, 6]` supported.
2. `state = load_state()` (dict of `ActionState`); `snapshot = load_ledger()`.
3. For each `action` in snapshot:
   - `is_action_due(action, state[id].last_run, now)` → skip if not; due but `now < state[id].retry_at()` → skip (backoff, no model call).
   - `_run_action(...)` runs `chat_loop.chat(messages)` in a thread executor.
   - Strip reply. An empty reply **without** the sentinel counts as a failed run (`n_failed`, warning logged, state untouched, one-shot stays active) — small local models sometimes return `""` after a tool round. If `SENTINEL` appears anywhere, remove it; if ≤`_ACK_MAX_CHARS` (300) chars remain → suppress notify (longer leftovers are sent with the sentinel stripped); still update state; for `kind=at`, archive to `completed`.
   - Otherwise resolve channel via `_resolve_channel(action.channel)` → `(target_scope, channels.Channel)` (falls back to `cfg.heartbeat_default_channel`).
   - On notify success: `mark_success` (stamps `last_run`, clears failures); for `kind=at`, archive. On any failure: `mark_failure` → exponential backoff (see State); the action stays active and retries once the backoff elapses. `Channel.notify` implementations MUST raise on non-delivery (Discord: heartbeat channel unset / unresolvable / `send` error via `_discord_send(..., raise_on_error=True)`; Telegram: unparseable scope) — a silent `return` would be counted as delivered and archive the one-shot.
4. `prune_state(active ∪ completed ids)` — writes already happened per action; there is no bulk save.

Each action is wrapped in try/except — one bad action does not stop the others.

## System-prompt injection (`chat_loop.build_system_prompt`)

The `now` string passed to `build_system_prompt` always comes from `heartbeat.format_now()` (`2026-10-20 15:30 EEST (UTC+0300)` — local wall clock **with** offset) so the model can emit tz-aware `at` values; every frontend session bootstrap, `_refresh_system_prompt`, the mid-loop rebuild and `_build_action_messages` use it. Do not reintroduce a bare `datetime.now().strftime(...)`.

After memory summary, `heartbeat.summary()` is appended:

```
Active heartbeat actions:
ab12cd [at]    → cli            | at 2026-05-08T10:30:00+00:00 | Remind me to check the kettle
ef34gh [cron]  → telegram-12345 | mon,thu 07:00 Europe/Helsinki | Summarise overnight email
ij56kl [every] → cli            | every 1800s                   | Watch high-priority email
```

Truncated at 400 chars with a `(call list_heartbeat_actions for full list)` hint. `chat_loop._MEMORY_MUTATORS` includes `create_heartbeat_action` / `cancel_heartbeat_action`, so the system prompt refreshes mid-loop after either tool runs — the model sees its own scheduling within the same turn.

## Scope ContextVar (`chat_loop.current_scope`)

Each frontend wraps its `chat()` call in `scoped_chat(scope, lambda: chat(messages))`:
- CLI → `"cli"`
- Telegram → `common.telegram_scope(chat_id)`
- Discord → `common.discord_scope(channel_id)`

`create_heartbeat_action` reads `chat_loop.current_scope.get()` to auto-fill the action's `channel` and `created_from` when the model omits them. Falls back to `cfg.heartbeat_default_channel`. The model never has to know which surface it's on.

Heartbeat ticks themselves run with `current_scope` set to `None` — actions carry their channel explicitly.

## Sentinel

`SENTINEL = "HEARTBEAT_OK"` (`heartbeat.py`). Detection: sentinel anywhere in the reply (leading, trailing, wrapped in `**…**`). After removing it, ≤300 leftover chars = ack chatter → suppressed; longer = real alert, delivered with the sentinel stripped.

## Routing (`channels.for_scope`)

- `cli` → `CHANNELS["cli"]`
- `telegram-<id>` → `CHANNELS["telegram"]`; channel parses `chat_id` from scope to send.
- `discord-<id>` → `CHANNELS["discord"]`; channel currently **ignores** the id and posts to `cfg.discord_heartbeat_channel`. (Greenfield deployment — Discord per-channel routing TBD.)

## CLI heartbeat printing

`_CLIChannel.notify` uses `prompt_toolkit.application.run_in_terminal` to print without disrupting the active prompt.

## `/heartbeat` slash

`common.run_heartbeat(send, scope, sub)`:
- `sub == "run"` → `heartbeat.spawn_tick()` (strong-ref'd background task; returns None while a tick is in flight → reply `"heartbeat tick already running"`), else reply `"heartbeat tick fired"`.
- Else → prints `enabled / interval / default_channel`, then active actions (one per line) and the last 5 completed.
- `warn_no_discord_channel=True` (Discord only) adds a notice if `cfg.discord_heartbeat_channel is None`.

## Tools

| tool | purpose |
|------|---------|
| `create_heartbeat_action(kind, prompt, at?, schedule?, every?, channel?)` | Append an action. `channel` defaults to current frontend scope, then `cfg.heartbeat_default_channel`. |
| `list_heartbeat_actions(include_completed=False)` | Read ledger. |
| `cancel_heartbeat_action(action_id)` | Pop active action. Returns `status='not_found'` if id unknown. |

All three are auto-registered via `tools/__init__.py → from .heartbeat_tools import …`. The model is told (in `_SYSTEM_PROMPT_BASE`) to use these for reminders/scheduled checks/watch requests instead of stuffing them into memory.
