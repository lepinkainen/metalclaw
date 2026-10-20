# testing

`pytest -q` (or `task test`). Python ≥ 3.14, `uv run pytest`.

## Layout

```
tests/
  conftest.py                 fixtures
  test_config.py              load order, env override, validators
  test_discord.py             _split_for_discord, _strip_bot_mention, _discord_should_respond
  test_escalation.py          chat_via_escalation, escalate_to_big_model tool
  test_frontends_common.py    parsers, formatters, run_remember/forget/memory
  test_inbox_history_usage.py inbox tool, history search/up-arrow isolation, usage, /status, Debouncer, Telegram debounce
  test_heartbeat.py           parse_heartbeat_file, is_due, run_tick, sentinel handling
  test_mcp_client.py          in-process MCPServer via monkeypatched _transport: register/call/timeout/truncation/isolation, config validators
  test_memory.py              parse/render, mutators, forget statuses, migration
  test_providers.py           chat-loop tool dispatch + session-provider stamping
  test_litellm_provider.py    LiteLLMProvider request/response shape, tool-call extraction
  test_registry_schema.py     @tool schema generation, Optional collapsing
  test_routing.py             /help, /train, /weather slash dispatch
  test_self_change.py         run_self_change orchestration (mocked subprocess)
  test_telegram_format.py     CommonMark → HTML edge cases
  test_tools_registration.py  every @tool present in TOOLS
  test_vault_search.py        ripgrep wrapper, path traversal guard
```

## Fixtures (`tests/conftest.py`)

- `_isolated_data_dir` (autouse) — points `XDG_DATA_HOME` at a tmp dir so `history.db`/state never touch the real data dir.
- `clear_env(monkeypatch)` — unsets all `_ENV_VARS` (Fastmail/Ollama/Discord/Telegram).
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

- Build `HeartbeatFile` directly or feed `parse_heartbeat_file(text)`.
- For `run_tick`, monkeypatch `chat_loop.chat` to return canned text. Assert state file updates and channel `notify` calls.

## Self-change tests

- Monkeypatch `subprocess.run` to simulate `claude -p` exit codes and `task lint/build/test` results.
- Use `monkeypatch.setattr("builtins.input", ...)` to drive the approve/reject prompt.

## Common stubs

```python
class StubProvider:
    name = "stub"

    def __init__(self, replies):
        self.replies = iter(replies)

    def chat_once(self, history, tools, system):
        return next(self.replies)
```

Pass to `_chat_with_provider(stub, Session(history=[UserMessage("hi")]))` to drive multi-turn tool-call sequences without an LLM. Replies are `AssistantMessage(text=..., tool_calls=[ToolCall(...)])`; the loop builds the `ToolResultMessage`s itself.

## CI gates (`task test` + `task lint` + `task build`)

- `task lint` — `ruff check .` + `ruff format --check .` + `mypy .` + optional gitleaks.
- `task build` — import smoke test (`python -c "import bot, tools, registry, self_change, memory, config"`). **Doesn't import providers/frontends** — those are runtime deps. If you break import in those, build won't catch it; run `task test` too.
- `task test` — full suite, suppresses output unless failure.

`run_self_change` runs all three sequentially; `approve` blocks on any failure unless `approve!`.
