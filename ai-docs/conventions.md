# conventions

House rules — observed in code, enforce when editing.

## Imports

- Absolute, **non-relative**. `from chat_loop import chat`, not `from .chat_loop import chat`.
- Group: stdlib → third-party → first-party. Alphabetize within group (ruff handles).
- Lazy-import inside fn body when the dep would cause a circular at module load. Examples: `bot` inside `tools.escalate_to_big_model` (`tools.py:784`), `bot` inside `heartbeat.run_tick` (`heartbeat.py:243`).

## Pydantic

- Tool argument models prefix with `_` (private to module): `_RollDieArgs`, `_WeatherArgs`, etc.
- Use `Field(description="...")` for tool-arg descriptions — they show up in the JSON schema and improve model tool-call quality.
- `Optional` collapses to non-`anyOf` schema via `_schema_from_model` — don't manually build schemas.

## Async

- Provider calls are blocking httpx/SDK — always dispatch via `loop.run_in_executor(None, lambda: chat(...))` from frontends and heartbeat.
- Don't `await` inside a tool function. Tools are sync; the loop wraps them.
- `asyncio.create_task(heartbeat.run_tick())` in `/heartbeat run` — fire-and-forget, errors logged inside `run_tick`.

## Locking

- All `memory.py` mutations through `_locked()` ctxmgr. Reads can use the cached `load()`.
- No other module needs a process lock; SQLite history is single-connection per `SQLiteHistory` instance.

## Errors

- Tools: raise normally. `chat_loop._run_tool` catches and stringifies (`f"Error: {e}"`).
- Frontends: catch in the message handler, post `"Error: {e}"` and `messages.pop()` to revert the user message.
- Config: missing required fields → `ValueError` with actionable message (mention env var + yaml field).

## Logging

- `logging.getLogger("metalclaw.<area>")` — namespaced loggers.
- `log.info("memory write op=%s key=%s", op, key)` — structured-ish lazy formatting; never f-strings inside `log.*`.
- Noisy libs silenced in `bot.main()`: `httpx`, `telegram*`, `discord*`.

## Comments

- Module docstrings only when the module's role isn't obvious from imports.
- Inline comments explain **why**, not what. Ones that exist (`bot.py:43-44`, `tools.py:782-783`) document non-obvious decisions.

## File operations

- Atomic writes: `tempfile.mkstemp(prefix=".x-", dir=path.parent)` → write → `os.replace`. See `memory.py:162`.
- JSON state at `$XDG_DATA_HOME/metalclaw/<file>.json` (`heartbeat_state.json`, `telegram_chats.json`).

## Frontend ergonomics

- CLI uses Rich (`rich.console.Console`, `rich.markdown.Markdown`). Text strings pass through unchanged; markdown rendered.
- Telegram needs HTML conversion (`telegram_format.to_html`) — Telegram's MarkdownV2 is too restrictive. Always fall back to plain text on render error (`telegram.py:73`).
- Discord renders CommonMark natively; no conversion. Honor 2000-char limit via `_split_for_discord`.

## Naming

- Private symbols start with `_`. Module-private + test-importable helpers (e.g. `_chat_with_provider`) are still re-exported via `bot.__all__` for tests.
- Tool functions: snake_case, name = registry key.

## Tests

- Live under `tests/`. `pytest -q` is the canonical runner.
- `clear_env` + `cfg_file` + `write_config` fixtures in `tests/conftest.py`.
- After mutating env or yaml, call `config.reset_cache()`.
- For provider tests, use the recorded fixtures in `tests/test_providers.py`.

## Don't

- Don't add abstractions for hypothetical future providers/frontends — `chat_loop.py` is intentionally thin.
- Don't introduce relative imports.
- Don't read `cfg = get_config()` at module top-level — it forces import-time validation. Read inside functions.
- Don't mutate `messages` after `chat()` returns expecting the system message to stay in sync — call `_refresh_system_prompt` before each user turn.
