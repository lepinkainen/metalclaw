# ai-docs INDEX

AI-only architecture docs. Dense, indexed, cross-referenced. No human-friendly prose. Use file:line refs (e.g. `bot.py:33`) — paths relative to repo root unless noted.

## Files

| File | Load when |
|---|---|
| `architecture.md` | Need module graph, layer boundaries, import direction, lifecycle. |
| `modules.md` | Need per-module symbol map: exports, key fns, deps in/out. |
| `chat-loop.md` | Editing `chat_loop.py`, debugging tool-call loop, system prompt, escalation. |
| `tools.md` | Adding/modifying tools, slash-command wiring, schema gen. |
| `providers.md` | Adding/modifying provider, history shape, tool-result formatting. |
| `memory.md` | Editing `memory.py`, locking, parse/render, migration. |
| `frontends.md` | CLI/Telegram/Discord delta, session map, response gating. |
| `heartbeat.md` | Scheduler, scope file format, channel routing, sentinel. |
| `config.md` | Adding fields, env overrides, validation. |
| `dataflow.md` | Sequence diagrams: user turn, escalation turn, heartbeat tick, memory write. |
| `conventions.md` | House rules: imports, locking, async, errors, tests. |
| `gotchas.md` | Subtle traps: circular imports, mutated history, tool registration order, file-watching. |
| `testing.md` | pytest fixtures, conftest, common stubs. |

## Source map (line counts)

```
bot.py            134   entrypoint, arg parse, async orchestration
chat_loop.py      301   chat(), chat_via_escalation(), run_turn, _parse_command, system prompt
registry.py        63   @tool decorator, TOOLS dict, pydantic→json-schema
channels.py        52   Channel Protocol, scope→channel registry
config.py         180   pydantic Config, yaml load, env merge, lru_cache
memory.py         381   Memory dataclass, parse/render, file lock, mutators, forget
heartbeat.py      324   parse, schedule, run_tick, scope discovery
history.py         64   SQLiteHistory (prompt_toolkit subclass)
self_change.py    183   spawn `claude -p`, gate via task lint/build/test, approve/reject
live_tool.py      290   /add-tool: spawn `claude -p`, focused gates, async approval
telegram_format.py 146  CommonMark→Telegram HTML
vault_search.py   205   ripgrep wrapper + read_note
providers/base.py  40   Protocol, AssistantMessage, ToolCall
providers/__init__ 32   get_provider() factory
providers/ollama  71
providers/openai  65
providers/anthropic 83
tools/__init__.py  38   re-exports per-domain modules so @tool fires on `import tools`
tools/_http.py      8   shared httpx.Client
tools/dice.py      18
tools/escalation.py 51  escalate_to_big_model
tools/mail.py     364   Fastmail JMAP list_emails / read_email
tools/manual.py   181   manual page lookup
tools/memory_tools 106   five memory mutator/reader tools
tools/search.py    52   search_vault, read_note
tools/trains.py   110   train_departures
tools/weather.py  123
frontends/cli.py  331
frontends/common  448   shared parsers/formatters/runners + scope helpers
frontends/discord 308
frontends/telegram 287
```

## Lookup shortcuts

- Tool registration sequence → `gotchas.md#tool-registration`
- Where tests import private helpers from → `architecture.md#test-imports` (defining module, not `bot`)
- Memory locking model → `memory.md#locking`
- Discord vs Telegram message split → `frontends.md#message-splitting`
- Escalation message-history snapshot → `chat-loop.md#escalation` and `tools/escalation.py:33`
- Heartbeat sentinel → `heartbeat.py:29` `SENTINEL = "HEARTBEAT_OK"`
