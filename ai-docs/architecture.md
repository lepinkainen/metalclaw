# architecture

## Layers (top imports bottom; never reverse)

```
                   bot.py (entrypoint)
                       │
        ┌──────────────┼───────────────┐
        ▼              ▼               ▼
    frontends/      heartbeat.py   self_change.py
    cli telegram        │
    discord common      │
        │   │           │
        ▼   ▼           ▼
        chat_loop.py    │
        ▲   ▲   ▲       │
        │   │   │       │
        │  channels.py  │
        │   │   │       │
        ▼   ▼   ▼       ▼
        providers/  memory.py  tools/  vault_search.py  registry.py
        ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲
        └───┴───┴───┴───┴───┴───┴───┴───┴───┴───┘
                   config.py (leaf — only stdlib + pydantic + yaml)
```

Rules:
- `chat_loop.py` has **no frontend deps** — only providers, registry, memory, config.
- `tools/escalation.py` lazy-imports `chat_loop` + `providers` inside `escalate_to_big_model` (`tools/escalation.py:30`) to avoid the `chat_loop → registry → tools` cycle at module load.
- `heartbeat.py` lazy-imports `chat_loop` inside `run_tick` (`heartbeat.py:241`) for the same reason.
- Frontends know nothing of each other — they coordinate via `channels.py` registry.

## Lifecycle

`bot.py:96 main()`:
1. Parse argv (`--daemon`, `--no-telegram`, `--no-discord`).
2. Logging level: INFO if `--daemon` else WARNING. Silence noisy libs (httpx, telegram, discord).
3. `asyncio.run(_async_main(...))`.

`bot.py:30 _async_main()`:
1. **`import tools`** (`bot.py:33`) — triggers `@tool` decorators registering into `registry.TOOLS`. Must precede any `chat()` call.
2. `get_config()` (caches via `lru_cache`).
3. `memory.migrate_legacy_scopes()` — one-shot collapse of `cli.md`/`telegram-*.md`/`discord-*.md` → `memory.md`.
4. Conditionally start Telegram (`_start_telegram`) and Discord (`_start_discord`); each only if its token exists. Daemon mode requires ≥1 frontend token.
5. `heartbeat.run(stop_event)` as background task.
6. If not daemon → `run_cli_repl()`. Else `await asyncio.Event().wait()` until SIGINT.
7. Finally: signal `stop`, await heartbeat shutdown, stop telegram, stop discord.

## Test imports

Tests import directly from the module that defines the symbol — `chat_loop`, `frontends.cli`, `frontends.discord`, `frontends.telegram`, `frontends.common`, `memory`, etc. `bot.py` is a thin entrypoint with no re-exports; importing `bot._foo` will fail.

## Concurrency model

- One asyncio event loop drives everything.
- Each user turn runs through `chat_loop.run_turn` (`chat_loop.py:273`), which dispatches the blocking provider call via a single `loop.run_in_executor(None, chat_call)` (`chat_loop.py:296`). All three frontends (CLI `frontends/cli.py:167,318`; Telegram `frontends/telegram.py:239`; Discord `frontends/discord.py:270`) share that path. Heartbeat has its own executor call at `heartbeat.py:303`.
- Discord runs handlers concurrently per channel; `frontends/discord.py:24-33` keeps a per-channel `asyncio.Lock` to serialize session mutations. Telegram does not need one because PTB serializes updates per-chat by default (see comment near `frontends/telegram.py:256`).
- Memory mutations: per-process `threading.Lock` + `fcntl.flock` cross-process on sidecar `memory.md.lock` (`memory.py:179`).
- SQLite `history.db` (`history.py`): one process-wide shared connection (`check_same_thread=False`) guarded by `history._LOCK`; used by the CLI `SQLiteHistory`, Telegram/Discord turn recording, `search_conversation_history`, and `usage` recording from executor threads.

## Provider abstraction invariants

- Each session sticks to **one** provider — history shape is opaque to the rest of the app.
- `provider.chat_once(messages, tools, system)` returns `AssistantMessage(text, tool_calls, raw, usage)`; `usage` (`providers.base.Usage`) is recorded to the `usage` table by `_chat_with_provider`. `raw` is appended to history verbatim.
- `provider.format_tool_results(results)` returns a list of dicts to extend history with — provider chooses shape (Ollama/OpenAI: `role=tool` per result; Anthropic: single `role=user` block with `tool_result` blocks).

## Single-user invariant

`memory.py` has **no scope** — single shared file. Per-session conversation state still keyed by transport id (Telegram `chat_id`, Discord `channel.id`, CLI session timestamp). See `frontends.md#sessions`.
