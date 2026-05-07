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

## State: `$XDG_DATA_HOME/metalclaw/heartbeat_state.json`

```json
{"version": 1, "last_run": {"<action_id>": "<iso-8601>"}}
```

Per-action last-run timestamp. Rebuilt automatically on first tick if missing.

## Due logic (`heartbeat.is_action_due`)

| kind | due iff |
|------|---------|
| `at` | `last_run_iso is None` AND `now >= parse_iso(at)` |
| `every` | first run OR `(now - last_run).total_seconds() >= every` |
| `cron` | local weekday in `schedule.days` AND `local_now >= scheduled_today` AND (no `last_run` OR `last_run < scheduled_today` in the same `tz`) |

`cron` uses `zoneinfo.ZoneInfo`. A missed schedule (server down at 07:00, tick at 09:00) still fires — better late than never — and updates `last_run` so it won't re-fire today.

## `run_tick` (`heartbeat.py:run_tick`)

1. Active-hours gate (`_within_active_hours(now.astimezone(), cfg.heartbeat_active_hours)`). Wraparound `[22, 6]` supported.
2. `state = load_state()`; `snapshot = load_ledger()`.
3. For each `action` in snapshot:
   - `is_action_due(action, state.get(action.id), now)` → skip if not.
   - `_run_action(...)` runs `chat_loop.chat(messages)` in a thread executor.
   - Strip reply. If `clean == SENTINEL` (or starts with it) → suppress notify; still update state; for `kind=at`, archive to `completed`.
   - Otherwise resolve channel via `_resolve_channel(action.channel)` → `(target_scope, channels.Channel)` (falls back to `cfg.heartbeat_default_channel`).
   - On notify success: update state; for `kind=at`, archive. On notify failure: leave action active (next tick retries).
4. `save_state(state)`.

Each action is wrapped in try/except — one bad action does not stop the others.

## System-prompt injection (`chat_loop.build_system_prompt`)

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

`SENTINEL = "HEARTBEAT_OK"` (`heartbeat.py`). Comparison: `clean == SENTINEL or clean.startswith(SENTINEL)` so trailing model chatter still suppresses.

## Routing (`channels.for_scope`)

- `cli` → `CHANNELS["cli"]`
- `telegram-<id>` → `CHANNELS["telegram"]`; channel parses `chat_id` from scope to send.
- `discord-<id>` → `CHANNELS["discord"]`; channel currently **ignores** the id and posts to `cfg.discord_heartbeat_channel`. (Greenfield deployment — Discord per-channel routing TBD.)

## CLI heartbeat printing

`_CLIChannel.notify` uses `prompt_toolkit.application.run_in_terminal` to print without disrupting the active prompt.

## `/heartbeat` slash

`common.run_heartbeat(send, scope, sub)`:
- `sub == "run"` → `asyncio.create_task(heartbeat.run_tick())`, reply `"heartbeat tick fired"`.
- Else → prints `enabled / interval / default_channel`, then active actions (one per line) and the last 5 completed.
- `warn_no_discord_channel=True` (Discord only) adds a notice if `cfg.discord_heartbeat_channel is None`.

## Tools

| tool | purpose |
|------|---------|
| `create_heartbeat_action(kind, prompt, at?, schedule?, every?, channel?)` | Append an action. `channel` defaults to current frontend scope, then `cfg.heartbeat_default_channel`. |
| `list_heartbeat_actions(include_completed=False)` | Read ledger. |
| `cancel_heartbeat_action(action_id)` | Pop active action. Returns `status='not_found'` if id unknown. |

All three are auto-registered via `tools/__init__.py → from .heartbeat_tools import …`. The model is told (in `_SYSTEM_PROMPT_BASE`) to use these for reminders/scheduled checks/watch requests instead of stuffing them into memory.
