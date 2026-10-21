# ai-docs INDEX

AI-only architecture docs. Dense, indexed, cross-referenced. No human-friendly prose. Use file:line refs (e.g. `bot.py:33`) — paths relative to repo root unless noted.

## Files

| File | Load when |
|---|---|
| `architecture.md` | Need module graph, layer boundaries, import direction, lifecycle. |
| `modules.md` | Need per-module symbol map: exports, key fns, deps in/out. |
| `chat-loop.md` | Editing `chat_loop.py`, debugging tool-call loop, system prompt, escalation. |
| `tools.md` | Adding/modifying tools, slash-command wiring, schema gen. |
| `providers.md` | Adding/modifying provider, canonical message model, per-provider `_to_wire`. |
| `memory.md` | Editing `memory.py`, locking, parse/render, migration. |
| `frontends.md` | Telegram/Discord delta, DB-backed sessions (`Session.load`/`reset`), response gating, network-error policy (fail fast at startup, warn at runtime). |
| `heartbeat.md` | Scheduler, scope file format, channel routing, sentinel. |
| `config.md` | Adding fields, env overrides, validation. |
| `dataflow.md` | Sequence diagrams: user turn, escalation turn, heartbeat tick, memory write. |
| `conventions.md` | House rules: imports, locking, async, errors, tests. |
| `gotchas.md` | Subtle traps: circular imports, session commit/persist ordering, tool registration order, turns-table growth. |
| `testing.md` | pytest fixtures, conftest, common stubs, session-persistence tests. |
| `mcp-plan.md` | Plan (phase 1 implemented — see Status): MCP client — external MCP server tools bridged into `registry.TOOLS`, config, lifecycle, sync→async dispatch, security. |
| `pipelines.md` | Watcher pipelines (`pipelines/`): context, trace, shared ledger, mail pipeline stages, heartbeat dispatch via `heartbeat_pipelines`. |
| `mail-pipeline-plan.md` | Plan (implemented 2026-10-21 — see Status): code-first mail pipeline design, rationale, build-vs-buy, test list. Mermaid chart. |

## Source map (line counts)

```
bot.py            224   headless entrypoint, arg parse (--no-telegram/--no-discord), logging, async orchestration
chat_loop.py      574   Session (load/reset/commit), chat(), chat_via_escalation(), run_turn, _parse_command, system prompt
registry.py       103   @tool decorator, TOOLS dict, pydantic→json-schema
channels.py        54   Channel Protocol, scope→channel registry (prefix-before-`-`)
config.py         252   pydantic Config, yaml load, env merge, lru_cache
memory.py         395   Memory dataclass, parse/render, file lock, mutators, forget
heartbeat.py     1240   ledger, schedule, run_tick, state rows, backoff, pipeline dispatch
history.py        600   shared locked sqlite: turns + legacy messages + usage + heartbeat_state + known_chats + reported_items + pipeline_runs
pipelines/       ~250   watcher pipelines: context.py (Outcome/Trace/Ledger/PipelineContext), mail.py (code-first mail check), REGISTRY
self_change.py     84   busy_slot()/is_busy()/SelfModificationBusy + git/subprocess helpers for live_tool
live_tool.py      385   /add-tool: spawn `claude -p`, focused gates, async approval, changes.jsonl
telegram_format.py 146  CommonMark→Telegram HTML
vault_search.py   230   ripgrep wrapper + read_note
mcp_client.py     325   MCP client: loop thread, server owner tasks, TOOLS bridge, result wrap
wilma.py          827   unofficial Wilma client: login/TOTP, students, messages, bulletins (news), subject matching
messages.py       156   canonical frozen messages, validate_exchange, message_to_dict/from_dict, SYSTEM_CACHE_BOUNDARY
providers/base.py  25   Provider Protocol (chat_once over Sequence[Message]); re-exports messages types
providers/__init__ 44   get_provider() factory
providers/ollama  186
providers/litellm 228   + prompt caching (system split, last-tool breakpoint)
tools/__init__.py  67   re-exports per-domain modules so @tool fires on `import tools`
tools/_http.py      8   shared httpx.Client
tools/conversation_history.py 51  search_conversation_history
tools/dice.py      22
tools/escalation.py 65  escalate_to_big_model
tools/heartbeat_tools.py 209  create/list/cancel_heartbeat_action
tools/inbox.py     52   append_to_inbox
tools/mail.py     783   Fastmail JMAP list/read/check/labels
tools/manual.py   193   manual page lookup
tools/memory_tools 120  five memory mutator/reader tools
tools/search.py    75   search_vault, read_note
tools/trains.py   133   train_departures
tools/weather.py  148
tools/wilma.py    247   list_wilma_messages, read_wilma_message, find_wilma_message, list_wilma_news, read_wilma_news
frontends/common 1146   COMMANDS spec, parsers/formatters/runners, scope helpers, Debouncer, status/usage, tool status, /add-tool pump
frontends/discord 417
frontends/telegram 471
```

No `frontends/cli.py`: the CLI REPL (prompt_toolkit + rich, `/self-edit`, `/think`) was removed; the bot is headless.

## Lookup shortcuts

- Tool registration sequence → `gotchas.md#tool-registration`
- Where tests import private helpers from → `architecture.md#test-imports` (defining module, not `bot`)
- Memory locking model → `memory.md#locking`
- Discord vs Telegram message split → `frontends.md#message-splitting`
- Escalation message-history snapshot → `chat-loop.md#escalation` and `tools/escalation.py:38`
- Session persistence (turns table, load/commit/reset) → `chat-loop.md#session`, `frontends.md#sessions`, `gotchas.md#turns-table-growth`
- Heartbeat sentinel → `heartbeat.py:29` `SENTINEL = "HEARTBEAT_OK"`
