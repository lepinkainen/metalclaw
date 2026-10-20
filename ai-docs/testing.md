# testing

`pytest -q` (or `task test`). Python ≥ 3.14, `uv run pytest`.

## Layout

```
tests/
  conftest.py                 fixtures
  test_config.py              load order, env override, validators
  test_discord.py             _split_for_discord, _strip_bot_mention, _discord_should_respond
  test_escalation.py          chat_via_escalation, escalate_to_big_model tool
  test_event_loop_offload.py  run_turn / run_tick keep flock + SQLite work off the loop thread
  test_frontends_common.py    parsers, formatters, run_remember/forget/memory
  test_inbox_history_usage.py inbox tool, search_messages over turns + legacy messages rows, usage, /status, Debouncer, Telegram debounce
  test_heartbeat.py           ledger, is_due, run_tick, sentinel, backoff, state rows, prompt clock/cache boundary
  test_known_chats.py         known_chats rows + legacy json import
  test_live_tool.py           /add-tool: delta validation, gates, approve/reject, __init__ merge, busy slot
  test_logging_setup.py       bot._setup_logging() writes the XDG state log
  test_mail_labels.py / test_mail_report.py / test_manual.py / test_wilma.py  tool-specific
  test_mcp_client.py          in-process MCPServer via monkeypatched _transport: register/call/timeout/truncation/isolation, config validators
  test_memory.py              parse/render, mutators, forget statuses, migration
  test_messages.py            canonical message dataclasses, validate_exchange
  test_ollama_provider.py / test_litellm_provider.py  provider _to_wire + response parsing (+ litellm cache_control shape)
  test_providers.py           chat-loop tool dispatch across providers
  test_registry_schema.py     @tool schema generation, Optional collapsing
  test_routing.py             _parse_command (/help, /add-tool, hyphenated commands, bare slash)
  test_self_change.py         busy_slot released after exception (only what is left of self_change)
  test_session_persistence.py turns table + DB-backed Session (see below)
  test_telegram_format.py     CommonMark → HTML edge cases
  test_telegram_split.py      4096-char split with HTML regrowth
  test_tool_loop_limits.py    iteration cap + repeat detection
  test_tool_status.py         format_tool_status, ToolStatusReporter
  test_tools_registration.py  every @tool present in TOOLS
  test_vault_search.py        ripgrep wrapper, path traversal guard
```

No CLI tests remain (no `prompt_toolkit`/`rich` stubs, no `run_self_change` subprocess mocks, no `builtins.input` patching).

## Fixtures (`tests/conftest.py`)

- `_isolated_data_dir` (autouse) — points `XDG_DATA_HOME` at a tmp dir so `history.db` (incl. the `turns` table) never touches the real data dir. Each test therefore starts with empty sessions.
- `_isolated_config` / `_default_config` (autouse) — write a minimal `vault_path`-only config and point `METALCLAW_CONFIG` (+ `XDG_CONFIG_HOME`) at it, so `get_config()` never reads the developer's real config; `reset_cache()` before and after.
- `clear_env(monkeypatch)` — unsets all `_ENV_VARS` (Fastmail/Ollama/Discord/Telegram/Wilma).
- `cfg_file(tmp_path, monkeypatch, clear_env)` — sets `METALCLAW_CONFIG=<tmp>/config.yaml`, calls `config.reset_cache()` before+after. Yields the path.
- `write_config()` — factory: `write_config(path, **fields)` writes yaml with `vault_path: <tmp>/vault` baseline.

Usage:
```python
def test_x(cfg_file, write_config):
    write_config(cfg_file, provider="ollama", model="llama3:latest")
    cfg = config.get_config()
    assert cfg.model == "llama3:latest"
```

## Provider tests

- Mock the client: monkeypatch `providers.ollama._CLIENT.post`, `providers.litellm_provider.litellm.completion`.
- Assert request shape (model, messages, tools, system position).
- Assert `_to_wire(history)` emits the exact wire dicts (that is what adapter tests verify); chat-loop tests use typed `messages.py` values instead.

## Memory tests

- Use `tmp_path` as vault; `write_config(cfg_file, vault_path=str(tmp_path))`.
- Test concurrent process scenario by spawning a subprocess that holds the lock — see existing patterns in `test_memory.py`.

## Heartbeat tests

- Create actions through `heartbeat.create_action(...)` against a tmp vault (`cfg` fixture in `tests/test_heartbeat.py`).
- For `run_tick`, monkeypatch `chat_loop.chat` to return canned text. Assert `heartbeat_state` rows and channel `notify` calls. Fake channels may use any `name`; the scope's prefix must match it for `for_scope` to resolve.

## Session persistence tests (`tests/test_session_persistence.py`)

- `message_to_dict`/`message_from_dict` round-trip for every message kind; unknown `kind` → `ValueError`.
- `history.turns_append/load/count/reset`: boundary semantics (reset empties the live history, keeps other scopes, pre-reset rows still hit `search_messages`).
- `Session.load(scope)` + `_chat_with_provider(stub, session)`: only the delta is appended (`_persisted`), a second `load` replays the same history, a real tool call's result row is stored.
- Failed turn: `run_turn` with a raising provider → `session.history == []` and `turns_load(scope) == []`.
- Scope-less `Session(system=...)` never reaches the DB.
- Pattern: monkeypatch `chat_loop.build_system_prompt` to a constant (`monkeypatch.setattr("chat_loop.build_system_prompt", lambda _now: "sys")`) so `Session.load` doesn't read memory/ledger — patch it on `chat_loop`, not on the frontend module (frontends no longer import it).

## Frontend turn tests

- Telegram/Discord `_run_text_turn` tests stub `chat_loop.chat` and let `Session.load` hit the isolated `history.db`; there is no session dict to pre-seed — insert history with `history.turns_append(scope, [...])` instead. The debounce test (`test_inbox_history_usage.py`) is the reference.

## Live-tool tests (`tests/test_live_tool.py`)

- `fake_repo` fixture = tmp git repo with a `tools/` package; `patch("subprocess.run")` simulates `claude -p` and the ruff gate; `clean_tools_dict` restores `registry.TOOLS`.
- Busy-slot regression: hold `self_change.busy_slot()` and assert `run_add_tool_live` aborts without spawning.

## Common stubs

```python
class StubProvider:
    name = "stub"

    def __init__(self, replies):
        self.replies = iter(replies)

    def chat_once(self, history, tools, system):
        return next(self.replies)
```

Pass to `_chat_with_provider(stub, Session(history=[UserMessage("hi")]))` (scope-less → in-memory) or to `Session.load("telegram-1")` (DB-backed) to drive multi-turn tool-call sequences without an LLM. Replies are `AssistantMessage(text=..., tool_calls=[ToolCall(...)])`; the loop builds the `ToolResultMessage`s itself.

## CI gates (`task test` + `task lint` + `task build`)

- `task lint` — `ruff check . --fix` + `ruff format .` + `mypy .` + optional gitleaks (autofixing; `task build-ci` is the non-mutating variant).
- `task build` — runs `lint` + `test`, then an import smoke test (`python -c "import bot, tools, registry, self_change, memory, config, mcp_client"`). **Doesn't import providers/frontends directly** (though `bot` pulls the frontends in). If you break import elsewhere, run `task test` too.
- `task test` — full suite, suppresses output unless failure.
- `task run` — `uv run python bot.py` (the old `task daemon` / `--daemon`).

`/add-tool`'s own gates are the focused ones in `live_tool._run_gates` (ruff on the new file, import, schema) — not the full `task` pipeline.
