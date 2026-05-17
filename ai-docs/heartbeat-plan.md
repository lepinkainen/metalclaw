Here’s a practical way to add it to this codebase.

## High-level idea

Add a small **scheduler/heartbeat subsystem** that runs alongside the CLI loop and periodically:

1. loads scheduled tasks
2. checks which tasks are due
3. turns each due task into a **synthetic user message**
4. sends that message through the existing `chat()` path
5. delivers the result via a **notification sink**
6. sleeps again if nothing is due

That lets scheduled work reuse the same tool-calling behavior the bot already has.

---

## Fit with current architecture

Right now:

- `bot.py` has the REPL and the `chat(messages)` tool loop
- `tools.py` contains callable tools
- `registry.py` auto-registers tools

So the cleanest design is:

- keep `chat()` as the execution engine
- add a new module like `heartbeat.py` for scheduling
- optionally add `calendar` / `notifications` tools or helper modules later

---

## Core design

## 1) Add a persistent task store

Create a file-backed store, probably SQLite or JSON.

I’d recommend **SQLite** because recurring tasks and run-state get fiddly quickly.

Example new file:

- `heartbeat.py` or `scheduler.py`
- DB file: `heartbeat.db`

Task record fields:

- `id`
- `name`
- `prompt`  
  - the exact synthetic user input to send to the bot
- `schedule_type`
  - `cron`
  - `interval`
  - maybe later `calendar_trigger`
- `schedule_expr`
  - e.g. cron string: `30 7 * * 1,4`
- `timezone`
  - e.g. `Europe/Helsinki`
- `enabled`
- `next_run_at`
- `last_run_at`
- `last_status`
- `notify_target`
  - stdout for now, maybe email/push later
- `dedupe_key` or `cooldown`
  - helps avoid repeated notifications
- `metadata_json`
  - extra task-specific config

For your examples:

- “check weather at 0730 every Monday and Thursday”
- “check R trains between 0700 and 0800 on office days”

both fit well.

---

## 2) Heartbeat loop

Add a background loop in `bot.py`:

- wake every N seconds, maybe 30 or 60
- ask scheduler for due tasks
- if none: sleep
- if some: execute them

Pseudo-shape:

```python
while running:
    due_tasks = scheduler.get_due_tasks(now)
    if not due_tasks:
        sleep(poll_interval)
        continue

    for task in due_tasks:
        run_scheduled_task(task)

    sleep(short_delay_or_poll_interval)
```

This is the “heartbeat”.

### Recommendation
Start with a **simple polling heartbeat** rather than a complex exact timer wheel. Polling every 30–60 seconds is plenty for these use cases.

---

## 3) Execute scheduled tasks as synthetic user input

This is the key reuse point.

You already have:

- `messages.append({"role": "user", "content": user_input})`
- `reply = chat(messages, ...)`

For a scheduled task, do the same thing, but mark it as system-generated.

Example synthetic message content:

```text
[SCHEDULED TASK: weather_rain_check]
Check the weather for Helsinki. If rain is expected today, notify the user briefly.
```

Better: use a dedicated helper:

```python
def run_prompt(prompt: str, source: str = "user") -> str:
    ...
```

Then both REPL and heartbeat can call the same function.

### Important
Use a **fresh message context per scheduled run**, or a separate per-task conversation history.  
Do **not** blindly share the live REPL conversation history with heartbeat jobs.

Why:

- scheduled jobs should be deterministic
- they shouldn’t inherit random chat context
- they may run while no one is at the terminal

A good pattern:

- include the normal system prompt
- add a small scheduling preamble
- add the task prompt
- call `chat()`

Example preamble:

```text
This message was triggered by a scheduled task. Carry it out as if the user had just asked it. If the task says to notify only on certain conditions, only produce a notification when those conditions are met.
```

---

## 4) Notification layer

You need a way to surface results when no one is actively chatting.

Start simple with a `notify(message: str)` abstraction.

Initial implementations:

- print to stdout
- append to a log file
- maybe desktop notification later
- maybe email later

For now:

- if the bot is running interactively, print:
  - `[scheduled] ...`
- if running headless later, notifications can go elsewhere

Pseudo:

```python
def notify(task, text):
    console.print(f"\n[bold yellow][scheduled:{task.name}] [/bold yellow]{text}\n")
```

---

## 5) Separate “check” tasks from “notify” tasks

Some scheduled prompts should not always notify.

Example:
> “check the weather at 0730 every monday and thursday and notify me if it's raining”

That should not emit a useless “no rain” message every time unless requested.

So each task should have a **delivery policy**, e.g.:

- `always`
- `only_if_nonempty`
- `only_if_changed`
- `only_if_condition_met`

Simplest implementation: encode this in the prompt and enforce a structured response.

Example scheduled prompt:

```text
Check the weather for Helsinki today.
If rain is expected, respond with a concise notification for the user.
If no rain is expected, respond with exactly: NO_NOTIFICATION
```

Then heartbeat logic can do:

- if reply == `NO_NOTIFICATION`: do nothing
- else notify

This is much more reliable than trying to infer intent from free text.

---

## 6) Add scheduled-task commands

In `bot.py`, add commands like:

- `/schedule-add`
- `/schedule-list`
- `/schedule-remove`
- `/schedule-pause`
- `/schedule-run <id>`
- `/schedule-help`

Examples:

```text
/schedule-add --cron "30 7 * * 1,4" "Check the weather for Helsinki. If rain is expected today, respond with a concise notification. Otherwise respond with NO_NOTIFICATION."
```

```text
/schedule-list
```

```text
/schedule-run weather_rain_check
```

This lets users manage tasks without self-editing code.

---

## 7) Scheduling format

Use **cron** first.

It covers the first use case immediately:

- `30 7 * * 1,4` = 07:30 every Monday and Thursday

For “between 0700 and 0800” you have two options:

### Option A: multiple scheduled checks
Run every 5 or 10 minutes:

- `0,10,20,30,40,50 7 * * 1-5`

and let the task decide whether today is an office day.

### Option B: richer schedule model
Support interval windows:
- on office days, every 5 min between 07:00 and 08:00

I’d start with Option A. Simpler and good enough.

---

## 8) Calendar-aware tasks

Your second use case needs calendar data:

> when I have an office day marked on my calendar, notify me of any R trains that are late or cancelled between 0700 and 0800

That suggests adding either:

### A. A new tool
Example:
- `calendar_events(date=..., query=...)`
- or `is_office_day(date=...)`

Then the scheduled prompt can use the tool naturally.

Example prompt:

```text
Check whether today is marked as an office day in my calendar.
If not, respond with NO_NOTIFICATION.
If it is an office day, check current R train departures and notify me only if any trains between 07:00 and 08:00 are delayed or cancelled. Otherwise respond with NO_NOTIFICATION.
```

### B. A precondition on the task itself
Store a task precondition like:
- `calendar_tag = "office day"`

But that gets domain-specific fast. Better to let the bot reason with a calendar tool.

### Recommendation
Add a **calendar tool** later, not into the first heartbeat slice.

---

## 9) Prevent duplicate alerts

This matters a lot for polling-based schedules.

Example:
- every 5 minutes between 07:00–08:00
- same train remains delayed
- bot keeps notifying repeatedly

So each task should track dedupe state:

- last notification text hash
- last notification time
- last seen condition ids

For trains, a dedupe key might be based on:
- line
- scheduled departure
- status

Then only notify if:
- status changed, or
- it’s a newly affected train

If you want to keep v1 simple, use a generic cooldown:
- “don’t send identical notification text more than once in 60 minutes”

That’s not perfect, but good enough to start.

---

## 10) Failure handling

Scheduler needs to survive errors cleanly.

For each run, store:

- `started_at`
- `finished_at`
- `status`
- `error`
- `reply`
- `notification_sent`

If a tool call fails:

- mark run failed
- do not crash the heartbeat loop
- optionally notify only on repeated failures

Also guard against double execution:

- mark task claimed/running before executing
- update `next_run_at` atomically

---

## Recommended implementation plan

## Phase 1: minimal heartbeat infrastructure

Add:

- `scheduler.py` or `heartbeat.py`
- SQLite-backed task store
- cron schedule support
- polling loop
- synthetic prompt execution
- stdout notifications
- commands:
  - `/schedule-add`
  - `/schedule-list`
  - `/schedule-remove`
  - `/schedule-run`

This is enough for the weather example.

### Minimal task example
Name: `weather_rain_check`

Cron: `30 7 * * 1,4`

Prompt:

```text
Check the weather for Helsinki.
If rain is expected today, reply with a short notification for the user.
Otherwise reply exactly NO_NOTIFICATION.
```

---

## Phase 2: shared execution helper

Refactor `bot.py` so both:

- REPL user input
- scheduled tasks

go through a common function, something like:

```python
def run_bot_turn(user_input: str, source: str = "interactive") -> str:
    ...
```

This avoids duplicating chat/tool-call logic.

---

## Phase 3: structured scheduled responses

To make scheduling robust, ask scheduled tasks to return structured JSON, e.g.:

```json
{
  "notify": true,
  "message": "Rain expected in Helsinki today. Bring a rain jacket."
}
```

or

```json
{
  "notify": false
}
```

This is much safer than relying on arbitrary prose.

You can enforce it with a scheduling-specific preamble in the prompt.

---

## Phase 4: calendar integration

Add a calendar tool, probably one of:

- `calendar_events`
- `calendar_has_tag`
- `calendar_is_office_day`

Then create a scheduled task like:

- cron: every 10 min from 07:00–08:00 on weekdays
- prompt checks office-day calendar state first
- only then checks `train_departures`

Example prompt:

```text
First determine whether today is an office day on my calendar.
If not, respond with {"notify": false}.

If yes, check R train departures relevant for the 07:00–08:00 commute.
If any are delayed or cancelled, respond with {"notify": true, "message": "..."}.
Otherwise respond with {"notify": false}.
```

---

## Suggested file changes

## New files

- `heartbeat.py`
  - scheduler loop
  - due-task lookup
  - task execution
  - notification dispatch

Possibly also:

- `schedule_store.py`
  - SQLite persistence
- `tests/test_heartbeat.py`
- `tests/test_schedule_parsing.py`

## Changes to `bot.py`

- start heartbeat thread/task at startup
- add schedule management commands
- extract shared execution helper from REPL path

## Optional later

- `calendar_tools.py` or add calendar functions into `tools.py`

---

## Concurrency model

Because this is a CLI bot, simplest is:

- REPL on main thread
- heartbeat on background thread

Use a lock around shared console printing if needed.

More important: avoid sharing mutable `messages` history between REPL and scheduled tasks.

Each scheduled run should use its own message list.

---

## Example internal API

Something like this:

```python
@dataclass
class ScheduledTask:
    id: str
    name: str
    prompt: str
    cron: str
    timezone: str
    enabled: bool
    next_run_at: datetime
    notify_policy: str
```

```python
class Scheduler:
    def get_due_tasks(self, now: datetime) -> list[ScheduledTask]: ...
    def mark_started(self, task_id: str, now: datetime) -> None: ...
    def mark_finished(self, task_id: str, now: datetime, result: str) -> None: ...
    def compute_next_run(self, task: ScheduledTask, now: datetime) -> datetime: ...
```

```python
def run_scheduled_task(task: ScheduledTask) -> None:
    reply = run_synthetic_prompt(task.prompt)
    if should_notify(task, reply):
        notify(task, reply)
```

---

## One important product decision

Decide whether the heartbeat is:

### 1. Only active while the bot process is running
Simplest. If the CLI isn’t open, no checks happen.

### 2. A long-running daemon mode
Better for real usefulness.

You probably eventually want:

- `metalclaw`
  - interactive REPL
- `metalclaw --daemon`
  - heartbeat scheduler only
- maybe `metalclaw notify` or a status command

For a first version, “heartbeat only while running” is fine.

---

## My recommendation in one sentence

Build a **polling scheduler with a SQLite task store, cron-based schedules, isolated synthetic prompt execution, and simple notification delivery**, then add a calendar tool afterward for office-day logic.

---

If you want, I can turn this into a concrete implementation plan with:
- exact new commands
- DB schema
- module layout
- and a step-by-step patch plan for `bot.py` and new files.
