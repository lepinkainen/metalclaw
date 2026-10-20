# conventions

House rules — observed in code, enforce when editing.

## Imports

- Absolute, **non-relative**. `from chat_loop import chat`, not `from .chat_loop import chat`.
- Group: stdlib → third-party → first-party. Alphabetize within group (ruff handles).
- Lazy-import inside fn body when the dep would cause a circular at module load. Examples: `chat_loop` + `providers` inside `tools.escalate_to_big_model` (`tools/escalation.py:35`), `chat_loop` inside `heartbeat.run_tick` (`heartbeat.py:823`) and `_build_action_session` (`heartbeat.py:739`).

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

- Every module: `log = logging.getLogger(__name__)` (heartbeat/memory use `metalclaw.*` names; both reach the root handlers).
- Lazy %-formatting only (`log.info("x=%s", x)`), never f-strings. Truncate big payloads with `%.500s`.
- Levels: INFO = decisions/outcomes/one-line summaries (turn start/end, tool call + duration, HTTP latency); DEBUG = payload detail (args, raw responses, cache hits); WARNING = recoverable oddities (validation refusals, fallbacks); `log.exception` in every `except` that swallows or converts an error.
- NEVER log secrets: Fastmail token, bot tokens, API keys, Authorization headers. Message text / email subjects are fine. Log env-override *names*, not values.
- `bot._setup_logging`: root at DEBUG; rotating file `$XDG_STATE_HOME/metalclaw/metalclaw.log` (default `~/.local/state/metalclaw/metalclaw.log`, 5 MB x 5) always on at DEBUG; console INFO in `--daemon`, WARNING in REPL, override via `METALCLAW_LOG_LEVEL`. Console shows one-line errors (no tracebacks); tracebacks are file-only.
- Noisy libs (`httpx`, `httpcore`, `telegram`, `discord`, `litellm`, `asyncio`) pinned to WARNING in `_setup_logging`.

## Comments

- Module docstrings only when the module's role isn't obvious from imports.
- Inline comments explain **why**, not what. Ones that exist (`bot.py:43-44`, `tools.py:782-783`) document non-obvious decisions.

## File operations

- Atomic writes: `tempfile.mkstemp(prefix=".x-", dir=path.parent)` → write → `os.replace`. See `memory.py:162`.
- No JSON state files. Per-record runtime state goes into `history.db` as a table with row-level upserts and a JSON `meta` column for debugging context (see `heartbeat_state`, `known_chats`). Legacy JSON files (`heartbeat_state.json`, `telegram_chats.json`) are imported once on first use and renamed `.bak`.

## Frontend ergonomics

- CLI uses Rich (`rich.console.Console`, `rich.markdown.Markdown`). Text strings pass through unchanged; markdown rendered.
- Telegram needs HTML conversion (`telegram_format.to_html`) — Telegram's MarkdownV2 is too restrictive. Always fall back to plain text on render error (`telegram.py:73`).
- Discord renders CommonMark natively; no conversion. Honor 2000-char limit via `_split_for_discord`.

## Naming

- Private symbols start with `_`. Module-private helpers used by tests (e.g. `_chat_with_provider`) are imported directly from their defining module (`from chat_loop import _chat_with_provider`); `bot.py` does not re-export.
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
- Don't expect `session.system` to stay in sync with memory on its own — `run_turn` calls `_refresh_system_prompt(session)` before each user turn; do the same if you drive `chat()` directly.
