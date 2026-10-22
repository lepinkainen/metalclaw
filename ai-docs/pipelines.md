# pipelines — watcher pipelines (as built)

Design + rationale: `mail-pipeline-plan.md`. This file is the implementation reference.

Shape: cheap API poll → deterministic filters → model only when something survives → deliver → acknowledge. Plain async functions; no workflow library (plan § "Why no workflow library"). Three concerns kept apart: **pipeline** (`pipelines/<source>.py`), **runner** (`heartbeat.py`), **context** (`pipelines/context.py`).

## Package

| File | Contents |
|---|---|
| `pipelines/__init__.py` | `REGISTRY: dict[str, Pipeline] = {"mail": mail.run}`; `Pipeline = Callable[[PipelineContext], Awaitable[Outcome]]`. The registry dispatches whole pipelines, never steps. |
| `pipelines/context.py` | `Outcome`, `Step`, `Trace`, `Ledger`, `PipelineContext`, `default_ask_model`. |
| `pipelines/mail.py` | `run(ctx)`, `SOURCE = tools.mail.LEDGER_SOURCE` (`"mail"`), `TOOLS`, `SYSTEM`. |

## `pipelines/context.py`

- `Outcome` (StrEnum): `suppressed` (nothing survived the code filters, zero tokens), `dismissed` (model saw the batch, answered `HEARTBEAT_OK`), `delivered` (summary reached the channel). Failure is an exception, not an outcome.
- `Trace.step(name)` context manager → `Step(name, ms, note)`; `Step.set_note(text)`; `Trace.as_rows()` → `[{name, ms, note?}]`. Only appends to a list; persistence is the runner's job.
- `Ledger.drop_reported(source, items)` / `Ledger.mark(source, items, outcome)` — thin wrappers over `history.reported_ids` / `history.reported_mark`; `items` are dicts with an `id` key.
- `PipelineContext(config, now, channel, trace, ledger, ask_model, deliver)`.
  - `await ctx.ask_model(system=, user=, tools=) -> str`: `default_ask_model(now_text)` builds a scope-less `chat_loop.Session` with `build_system_prompt(now_text) + "\n\n" + system`, appends `user`, runs `chat_loop.chat(session, only_tools=frozenset(tools))` in `asyncio.to_thread`. Never touches the `turns` table.
  - `await ctx.deliver(text)`: provided by the runner; raises on non-delivery.

## `pipelines/mail.py` — stages

| Step (trace name) | Code | Note recorded |
|---|---|---|
| `fetch` | `tools.mail.migrate_legacy_ledger()` then `tools.mail.fetch_unread_heads()` — **uncapped**, oldest first, `Email/query` (filter `AND(inMailboxOtherThan[trash,junk,drafts,sent], notKeyword $seen)`, `sort receivedAt asc`, paged by `_HEADS_PAGE=500` via `position`) chained into `Email/get` with properties `id, receivedAt, mailboxIds` only. Returns `[{id, received_at, mailbox_ids}]`. | `N unread` |
| `labels` | `tools.mail.drop_labelled(heads, cfg.mail_ignore_labels)` — resolves names via `_fm_mailboxes()["by_path"]` then `["by_name"]` (lower-cased); unknown names warn + ignored; returns `(kept, {label: dropped_count})`. | `dropped N (Label n, …)` |
| `ledger` | `ctx.ledger.drop_reported("mail", heads)`. | `dropped N, M left` |
| — | empty → `Outcome.SUPPRESSED`. | |
| `bodies` | `heads[:cfg.mail_pipeline_max_batch]` (**cap after filtering**: a backlog of filtered/reported mail can never shadow newer mail; overflow rolls to the next run because the batch is marked before then), `tools.mail.fetch_bodies(ids)` (`Email/get` with `_SUMMARY_PROPERTIES`, same row shape as `list_emails`), `attach_handler_hints` (Wilma `hint` when `cfg.wilma_url`). All heads gone between fetches → `SUPPRESSED`. | `N fetched[, M deferred]` |
| `model` | `ctx.ask_model(system=SYSTEM, user=_user_message(emails), tools=TOOLS)`; `TOOLS = {read_email, find_wilma_message, list_wilma_news, read_wilma_news, add_email_label, remove_email_label}` — **no** `mark_emails_reported`/`check_new_mail`. User message = count line + JSON rows (`id, subject, from, received_at, preview, folders, hint`). | `N chars` |
| — | `heartbeat.strip_sentinel(reply)`: acked + empty → `mark` as `dismissed`, return `DISMISSED`. Empty without sentinel → `RuntimeError("empty reply without HEARTBEAT_OK")` (failure, nothing marked). | |
| `deliver` | `ctx.deliver(text)`; raises → ids stay unreported (at-least-once; a send that succeeded server-side but raised client-side is re-delivered next run — duplicates accepted). | |
| `mark` | `ctx.ledger.mark("mail", emails, "delivered")`. | |

Acknowledgment is **per batch**: `delivered` = the id was in a batch whose summary was delivered, `dismissed` = in a batch that got `HEARTBEAT_OK`. Neither claims per-mail mention. Both terminal.

## Runner (`heartbeat.py`)

- `HeartbeatAction.pipeline: str | None` — `kind` stays the schedule type (`at`/`cron`/`every`); `prompt` is `""`. `to_dict` emits `pipeline` only when set; `_action_from_dict` reads it; `_meta_for` adds `pipeline` to `heartbeat_state.meta`.
- `config_pipeline_actions()` — from `cfg.heartbeat_pipelines` (`config.PipelineAction`: `pipeline`, `every` | `schedule`, `channel?`, `id?`). Id defaults to `pipeline-<name>`, `created_from="config"`. Not written to `heartbeat.yaml`; config is the source of truth. Bad schedule → warning, entry skipped.
- `_run_tick_locked` iterates `snapshot.actions + config_pipeline_actions()`; due/backoff logic is shared. For `action.pipeline`: `_run_pipeline_action(action, now)` → `mark_success(last_outcome=<outcome>)`; exception → `_fail("pipeline failed: …")` + backoff. Unknown registry name raises **before** a run starts (no trace row; `heartbeat_state.meta.last_error` carries it). Prune keep-set includes pipeline ids.
- `_run_pipeline_action`: resolves channel (`_resolve_channel(action.channel)`), builds `_deliver` (raises if no channel), `PipelineContext`, `run_id = secrets.token_hex(8)`; `finally:` writes `history.pipeline_run_record(run_id, pipeline, action_id, started, finished, outcome|"failed", error, trace.as_rows())` and logs one line with all steps.
- `strip_sentinel(reply) -> (text, acked)` — extracted from the prompt path; both paths use it.
- `summary()` shows `pipeline:<name>` where a prompt would be.

## history.db

- `reported_items(source, external_id, outcome, first_reported_at)` PK `(source, external_id)`. `reported_ids(source, ids) -> set` (membership), `reported_mark(source, ids, outcome, ts=None)` (`INSERT OR IGNORE`, first outcome wins, dedups ids, drops empties), `reported_count(source)`.
- `pipeline_runs(run_id PK, pipeline, action_id, started_at, finished_at, outcome, error, steps JSON)`; `pipeline_runs_latest()` (newest per pipeline), `pipeline_runs_recent(pipeline, limit=10)`.
- Legacy `mail_reported.sqlite3` → `tools.mail.migrate_legacy_ledger()` (module flag `_legacy_ledger_checked`; rows become `delivered`, file renamed `.bak`). Called by `check_new_mail`, `mark_emails_reported` and the pipeline's `fetch` step.

## Config (`config.py`)

`heartbeat_pipelines: tuple[PipelineAction, ...]`, `mail_ignore_labels: tuple[str, ...]`, `mail_pipeline_max_batch: int = 50`, `mail_pipeline_instructions: str = ""` (appended to `SYSTEM` as "User's mail rules"). See `config.md`.

## `/status` (`frontends/common.format_status`)

After the heartbeat actions: `pipelines: N configured` then one line per config pipeline action — `• <id> [<kind>] next: <when> | last: <outcome> <started[:16]> (<error[:60]>)` or `no runs yet`.

## Adding a pipeline

1. `pipelines/<source>.py` with `async def run(ctx: PipelineContext) -> Outcome`; wrap each stage in `ctx.trace.step(...)`; use `ctx.ledger.drop_reported(SOURCE, …)` / `ctx.ledger.mark(SOURCE, …)` with a unique `SOURCE`; call `ctx.ask_model` with a narrow `tools` set and a system text that tells the model to answer `HEARTBEAT_OK` when nothing matters; `await ctx.deliver(text)` **before** marking.
2. Register in `pipelines/__init__.py:REGISTRY`.
3. Schedule under `heartbeat_pipelines` in config.yaml.
4. Tests: `tests/test_pipelines.py` has the pattern (`_ctx` builds a context with stubbed `ask_model`/`deliver`).

## Gotchas

- Pipelines run inside the heartbeat tick (`heartbeat.run` sleeps `heartbeat_interval_seconds` between ticks, default 1800), so a pipeline's `every` is effectively `max(every, heartbeat_interval_seconds)`: `every: 5m` needs `heartbeat_interval_seconds: 300`. Ticks are cheap (non-due actions cost no model call). `heartbeat_active_hours` gates pipelines too; the first tick inside the window picks up the overnight backlog.

- `pipelines.mail` imports `heartbeat.strip_sentinel` lazily inside `run` (heartbeat imports `pipelines` lazily too) — keep both lazy.
- Config pipeline actions have `created_at=""`; nothing reads it for them, but don't start.
- `Outcome` values are stored as strings in `heartbeat_state.meta.last_outcome` and `pipeline_runs.outcome`; `"failed"` is the runner's word, not an `Outcome` member.
- The cap must stay **after** the ledger filter (plan § Tests: starvation).
