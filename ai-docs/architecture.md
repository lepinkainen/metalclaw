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
        providers/  memory.py  tools.py  vault_search.py  registry.py
        ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲
        └───┴───┴───┴───┴───┴───┴───┴───┴───┴───┘
                   config.py (leaf — only stdlib + pydantic + yaml)
```

Rules:
- `chat_loop.py` has **no frontend deps** — only providers, registry, memory, config.
- `tools.py` lazy-imports `bot` + `providers` inside `escalate_to_big_model` (`tools.py:784`) to avoid circular at module load.
- `heartbeat.py` lazy-imports `bot` inside `run_tick` (`heartbeat.py:243`) for the same reason.
- Frontends know nothing of each other — they coordinate via `channels.py` registry.

## Lifecycle

`bot.py:127 main()`:
1. Parse argv (`--daemon`, `--no-telegram`, `--no-discord`).
2. Logging level: INFO if `--daemon` else WARNING. Silence noisy libs (httpx, telegram, discord).
3. `asyncio.run(_async_main(...))`.

`bot.py:61 _async_main()`:
1. **`import tools`** — triggers `@tool` decorators registering into `registry.TOOLS`. Must precede any `chat()` call.
2. `get_config()` (caches via `lru_cache`).
3. `memory.migrate_legacy_scopes()` — one-shot collapse of `cli.md`/`telegram-*.md`/`discord-*.md` → `memory.md`.
4. Conditionally start Telegram (`_start_telegram`) and Discord (`_start_discord`); each only if its token exists. Daemon mode requires ≥1 frontend token.
5. `heartbeat.run(stop_event)` as background task.
6. If not daemon → `run_cli_repl()`. Else `await asyncio.Event().wait()` until SIGINT.
7. Finally: signal `stop`, await heartbeat shutdown, stop telegram, stop discord.

## Re-exports

`bot.py` re-exports private helpers (`_parse_command`, `_chat_with_provider`, `_active_session_messages`, `_split_system`, `_run_tool`, `_DiscordChannel`, `_discord_scope_for`, `_strip_bot_mention`, `_split_for_discord`, `_DISCORD_MAX_MESSAGE`, `build_system_prompt`, `chat`) so the test suite keeps importing them as `bot._foo`. **Don't delete the re-exports** without updating tests. See `bot.py:43-58`.

## Concurrency model

- One asyncio event loop drives everything.
- Provider calls are **blocking httpx/SDK calls** dispatched via `loop.run_in_executor(None, lambda: chat(messages))` from each frontend (CLI: `cli.py:262`; TG: `telegram.py:222`; Discord: `discord.py:248`; heartbeat: `heartbeat.py:300`).
- Memory mutations: per-process `threading.Lock` + `fcntl.flock` cross-process on sidecar `memory.md.lock` (`memory.py:179`).
- SQLite history: single connection per `SQLiteHistory` (`history.py:14`); `prompt_toolkit` calls it from the loop thread.

## Provider abstraction invariants

- Each session sticks to **one** provider — history shape is opaque to the rest of the app.
- `provider.chat_once(messages, tools, system)` returns `AssistantMessage(text, tool_calls, raw)`. `raw` is appended to history verbatim.
- `provider.format_tool_results(results)` returns a list of dicts to extend history with — provider chooses shape (Ollama/OpenAI: `role=tool` per result; Anthropic: single `role=user` block with `tool_result` blocks).

## Single-user invariant

`memory.py` has **no scope** — single shared file. Per-session conversation state still keyed by transport id (Telegram `chat_id`, Discord `channel.id`, CLI session timestamp). See `frontends.md#sessions`.
