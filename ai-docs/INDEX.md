# ai-docs INDEX

AI-only architecture docs. Dense, indexed, cross-referenced. No human-friendly prose. Use file:line refs (e.g. `bot.py:64`) — paths relative to repo root unless noted.

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
bot.py            165   entrypoint, arg parse, async orchestration, re-exports
chat_loop.py      202   chat(), chat_via_escalation(), system prompt, _parse_command
registry.py        61   @tool decorator, TOOLS dict, pydantic→json-schema
channels.py        52   Channel Protocol, scope→channel registry
config.py         180   pydantic Config, yaml load, env merge, lru_cache
memory.py         381   Memory dataclass, parse/render, file lock, mutators, forget
heartbeat.py      322   parse, schedule, run_tick, scope discovery
history.py         64   SQLiteHistory (prompt_toolkit subclass)
self_change.py    173   spawn `claude -p`, gate via task lint/build/test, approve/reject
telegram_format.py 146  CommonMark→Telegram HTML
tools.py          805   tool implementations + schemas
vault_search.py   205   ripgrep wrapper + read_note
providers/base.py  40   Protocol, AssistantMessage, ToolCall
providers/__init__ 32   get_provider() factory
providers/ollama  71
providers/openai  65
providers/anthropic 83
frontends/cli.py  274
frontends/common  337   shared parsers/formatters/runners + scope helpers
frontends/discord 286
frontends/telegram 265
```

## Lookup shortcuts

- Tool registration sequence → `gotchas.md#tool-registration`
- Why tests import `bot._foo` → `architecture.md#re-exports`
- Memory locking model → `memory.md#locking`
- Discord vs Telegram message split → `frontends.md#message-splitting`
- Escalation message-history snapshot → `chat-loop.md#escalation` and `tools.py:787`
- Heartbeat sentinel → `heartbeat.py:29` `SENTINEL = "HEARTBEAT_OK"`
