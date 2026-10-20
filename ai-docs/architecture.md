# architecture

## Layers (top imports bottom; never reverse)

```
                   bot.py (headless entrypoint)
                       │
        ┌──────────────┼───────────────┐
        ▼              ▼               ▼
    frontends/      heartbeat.py   mcp_client.py
    telegram            │
    discord common      │
        │   │           │
        ▼   ▼           ▼
        chat_loop.py ── history.py (turns table via Session.load/commit/reset)
        ▲   ▲   ▲       │
        │   │   │       │
        │  channels.py  │
        │   │   │       │
        ▼   ▼   ▼       ▼
        providers/  memory.py  tools/  vault_search.py  registry.py
        ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲   ▲
        └───┴───┴───┴───┴───┴───┴───┴───┴───┴───┘
                   config.py (leaf — only stdlib + pydantic + yaml)

    frontends/common ── live_tool.py ── self_change.py (busy slot + git helpers)
```

Rules:
- `chat_loop.py` has **no frontend deps** — only providers, registry, memory, config, history (as `history_db`, for `Session` persistence), mcp_client (`system_prompt_note`).
- `history.py` imports `messages` (for `message_to_dict`/`message_from_dict`) and `config`; never `chat_loop`, `heartbeat` or a frontend.
- `mcp_client.py` imports only `registry` + `config` at module level; `mcp`/`httpx2` are imported lazily (~0.4 s import).
- `tools/escalation.py` lazy-imports `chat_loop` + `providers` inside `escalate_to_big_model` (`tools/escalation.py:30`) to avoid the `chat_loop → registry → tools` cycle at module load.
- `heartbeat.py` lazy-imports `chat_loop` inside `run_tick` (`heartbeat.py:241`) for the same reason.
- Frontends know nothing of each other — they coordinate via `channels.py` registry.

## Lifecycle

`bot.py:191 main()`:
1. Parse argv (`--no-telegram`, `--no-discord`). There is no `--daemon`: the process is always headless.
2. `_setup_logging()` (`bot.py:54`): root DEBUG, rotating file always DEBUG, console INFO unless `METALCLAW_LOG_LEVEL`. Silence noisy libs (httpx, telegram, discord, litellm).
3. `asyncio.run(_async_main(with_telegram=..., with_discord=...))`.

`bot.py:126 _async_main()`:
1. **`import tools`** (`bot.py:127`) — triggers `@tool` decorators registering into `registry.TOOLS`. Must precede any `chat()` call.
2. `get_config()` (caches via `lru_cache`).
3. `memory.migrate_legacy_scopes()` (`bot.py:130`) — one-shot collapse of legacy `cli.md`/`telegram-*.md`/`discord-*.md` → `memory.md`.
4. `await asyncio.to_thread(mcp_client.start, cfg.mcp_servers)` — bounded by max `startup_timeout_seconds`, never raises; registers MCP tools before any frontend turn.
5. Conditionally start Telegram (`_start_telegram`) and Discord (`_start_discord`); each only if its token exists. At least one frontend token is required, else `RuntimeError`.
6. `heartbeat.run(stop_event)` as background task (`bot.py:170`).
7. `await asyncio.Event().wait()` (`bot.py:175`) until SIGINT / Ctrl-C.
8. Finally: signal `stop`, await heartbeat shutdown, `mcp_client.stop()` (closes sessions, pops MCP tools, joins thread), stop telegram, stop discord.

Conversation state is not part of process lifecycle: sessions live in the `turns` table and are reloaded per turn (`frontends.md#sessions`), so a restart loses nothing but in-flight turns.

## Test imports

Tests import directly from the module that defines the symbol — `chat_loop`, `frontends.discord`, `frontends.telegram`, `frontends.common`, `memory`, `history`, etc. `bot.py` is a thin entrypoint with no re-exports; importing `bot._foo` will fail.

## Concurrency model

- One asyncio event loop drives everything — except MCP: `mcp_client` owns a second loop on daemon thread `mcp` (only spawned when a server is enabled). Sync tool closures `run_coroutine_threadsafe(..., _LOOP).result(timeout)` onto it from executor threads (or the main loop thread — can't deadlock since MCP I/O runs elsewhere). One owner task per server holds the `Client` context (anyio: enter/exit in the same task).
- Each user turn runs through `chat_loop.run_turn` (`chat_loop.py:534`), which dispatches the system-prompt refresh + blocking provider call via a single `loop.run_in_executor(None, _work)` (`chat_loop.py:568`). Both frontends (Telegram `frontends/telegram.py:399 _run_text_turn`; Discord `frontends/discord.py:354 _run_text_turn`) share that path, and both first do `await asyncio.to_thread(Session.load, scope)` (SQLite read off the loop). Heartbeat has its own executor call at `heartbeat.py:773`.
- Discord runs handlers concurrently per channel; `frontends/discord.py:69 _session_lock` keeps a per-channel `asyncio.Lock`. Telegram has `frontends/telegram.py:110 _chat_lock` (debounced turns run in timer tasks outside PTB's per-chat queue). The locks serialise load→turn→commit per chat so two turns never both load the same history and double-append.
- Memory mutations: per-process `threading.Lock` + `fcntl.flock` cross-process on sidecar `memory.md.lock` (`memory.py:179`).
- SQLite `history.db` (`history.py`): one process-wide shared connection (`check_same_thread=False`) guarded by `history._LOCK`; used by `Session.load/commit/reset` (turns table, from `to_thread` and executor threads), `search_conversation_history`, `usage` recording from executor threads, the heartbeat scheduler's per-action state (`heartbeat_state`), and `known_chats`. There is no cross-process lock on the DB beyond SQLite's own; two processes sharing one scope would interleave turns.

## Provider abstraction invariants

- History is provider-agnostic: a session may switch provider between turns (or escalate) because history is canonical `messages.Message` values, which is also what the `turns` table stores (`message_to_dict`).
- `provider.chat_once(history, tools, system)` takes canonical `messages.Message` values and returns `AssistantMessage(text, tool_calls, usage)`; `usage` is recorded to the `usage` table by `_chat_with_provider` but is **not** persisted in `turns` (dropped by `message_to_dict`). The reply itself is appended to history (frozen, so shared with snapshots).
- Tool results become `ToolResultMessage(call_id, name, content)` entries via `Session.append_exchange`; each provider's private `_to_wire` renders them in its own envelope on the next call.

## Single-user invariant

`memory.py` has **no scope** — single shared file. Per-session conversation state is still keyed by transport scope (`telegram-<chat_id>`, `discord-<channel_id>`) in the `turns` table. See `frontends.md#sessions`.
