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
  test_heartbeat.py           parse_heartbeat_file, is_due, run_tick, sentinel handling
  test_memory.py              parse/render, mutators, forget statuses, migration
  test_providers.py           ollama, openai, anthropic — request shape + raw round-trip
  test_registry_schema.py     @tool schema generation, Optional collapsing
  test_routing.py             /help, /train, /weather slash dispatch
  test_self_change.py         run_self_change orchestration (mocked subprocess)
  test_telegram_format.py     CommonMark → HTML edge cases
  test_tools_registration.py  every @tool present in TOOLS
  test_vault_search.py        ripgrep wrapper, path traversal guard
```

## Fixtures (`tests/conftest.py`)

- `clear_env(monkeypatch)` — unsets all `_ENV_VARS` (Fastmail/Ollama/OpenAI/Anthropic/Discord/Telegram).
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

- Mock the client: monkeypatch `providers.ollama._CLIENT.post`, `openai_provider.OpenAI`, `anthropic_provider.Anthropic`.
- Assert request shape (model, messages, tools, system position).
- Assert `AssistantMessage.raw` round-trips through `format_tool_results` correctly.

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
    def __init__(self, replies): self.replies = iter(replies)
    def chat_once(self, messages, tools, system):
        return next(self.replies)
    def format_tool_results(self, results):
        return [{"role":"tool", "tool_call_id": c.id, "content": j} for c, j in results]
```

Pass to `_chat_with_provider(stub, messages)` to drive multi-turn tool-call sequences without an LLM.

## CI gates (`task test` + `task lint` + `task build`)

- `task lint` — `ruff check .` + optional gitleaks.
- `task build` — import smoke test (`python -c "import bot, tools, registry, self_change, memory, config"`). **Doesn't import providers/frontends** — those are runtime deps. If you break import in those, build won't catch it; run `task test` too.
- `task test` — full suite, suppresses output unless failure.

`run_self_change` runs all three sequentially; `approve` blocks on any failure unless `approve!`.
