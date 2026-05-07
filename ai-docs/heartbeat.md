# heartbeat

Proactive scheduler. Runs in `bot._async_main` as `asyncio.create_task(heartbeat.run(stop))`.

## File: `heartbeat-<scope>.md`

Located at `<vault>/<memory_subdir>/heartbeat-<scope>.md`. Scopes:
- `cli`
- `telegram-<chat_id>`
- `discord-<channel_id>`

Format (frontmatter + body):

```markdown
---
tasks:
  - name: urgent-mail
    interval: 30m
    prompt: |
      Check inbox for unread mail using list_emails (mailbox=inbox, unread_only=true).
      Surface only genuinely urgent items. Else reply HEARTBEAT_OK.
  - name: weather
    interval: 6h
    prompt: ...
---

Free-form context appended to every tick.
```

- `interval`: int seconds OR `"30m"`/`"6h"`/`"1d"`/`"30s"` (regex `heartbeat.py:31`).
- Body: passed verbatim under "Additional context:" on every tick.
- Empty file or no file → scope opted out.
- Frontmatter alternatives accepted (`heartbeat.py:108 _split_yaml`):
  1. `---`-delimited frontmatter.
  2. ```` ```yaml ... ``` ```` fenced block.
  3. Bare YAML doc (whole file is YAML, body empty).
  4. Pure markdown (no tasks; body is whole text).

## Scope discovery

`discover_scopes()` (`heartbeat.py:191`):
- Globs `<memory_dir>/heartbeat-*.md` and extracts the suffix.
- Returns sorted list. Does **not** consult `channels.all_active_scopes()`.

## State

`$XDG_DATA_HOME/metalclaw/heartbeat_state.json`:
```json
{"version": 1, "last_run": {"<scope>::<task_name>": "<iso-8601>"}}
```

`is_due(state, scope, task, now)` (`heartbeat.py:170`):
- No prior run → due.
- Else due iff `(now - last).total_seconds() >= task.interval_seconds`.

## Run loop (`heartbeat.py:304`)

```
if not cfg.heartbeat_enabled: return
interval = max(30, cfg.heartbeat_interval_seconds)
while not stop.is_set():
    try: await run_tick()
    except Exception: log.exception(...)
    try: await asyncio.wait_for(stop.wait(), timeout=interval)
    except asyncio.TimeoutError: pass
```

## `run_tick` (`heartbeat.py:241`)

1. Active-hours check (`_within_active_hours(now.astimezone(), cfg.heartbeat_active_hours)`). Wraparound supported (e.g. `[22, 6]`).
2. Load state.
3. For each scope from `discover_scopes()`:
   a. Read + parse heartbeat file.
   b. Compute `due = [t for t in hb.tasks if is_due(...)]`. Skip if no due tasks AND no body.
   c. `_run_scope(...)` — builds messages, calls `bot.chat(messages)` via executor (blocking).
   d. Update `state[state_key(scope, t.name)] = now.isoformat()` for each due task.
   e. Strip reply; if `clean == SENTINEL` or starts with `SENTINEL` → silent (no notify).
   f. Else `channels.for_scope(scope).notify(scope, clean)`. Drop with warning if no channel.
4. `save_state(state)`.

## System-prompt augmentation

`_build_heartbeat_messages` (`heartbeat.py:216`) appends to the normal system prompt:

```
HEARTBEAT MODE: This is a scheduled wake-up, not a user message. Run only the
tasks listed below. Use tools as needed. If nothing requires the user's
attention, reply with exactly `HEARTBEAT_OK` and nothing else. Otherwise, reply
with a concise alert the user should see.
```

User content:
```
Heartbeat tick. Tasks due now:

- **<task1.name>**: <task1.prompt>
- **<task2.name>**: <task2.prompt>

Additional context:
<hb.body>
```

## Sentinel

`SENTINEL = "HEARTBEAT_OK"` (`heartbeat.py:29`). Comparison is `clean == SENTINEL or clean.startswith(SENTINEL)` so trailing model chatter doesn't break suppression.

## Routing back to channels

See `channels.py:39 for_scope`:
- `cli` → `CHANNELS["cli"]`.
- `telegram-*` → `CHANNELS["telegram"]`. Channel parses `chat_id` from scope to send.
- `discord-*` → `CHANNELS["discord"]`. Channel **ignores** the scope id and uses `cfg.discord_heartbeat_channel`.

## CLI heartbeat printing

`_CLIChannel.notify` (`cli.py:185`) uses `prompt_toolkit.application.run_in_terminal` to print without disrupting the active prompt.

## /heartbeat slash

`common.run_heartbeat(send, scope, sub)` (`common.py:275`):
- `sub == "run"` → `asyncio.create_task(heartbeat.run_tick())` and reply `"heartbeat tick fired"`.
- Else → describes config + lists tasks from the scope's file (or "no checklist" hint).
- `warn_no_discord_channel=True` (Discord only) appends a warning if `cfg.discord_heartbeat_channel is None`.
