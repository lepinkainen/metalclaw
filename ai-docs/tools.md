# tools

## Registration

`@tool(description=..., args=PydanticModel | None)` decorator (`registry.py:38`):
1. `args=None` → `_EMPTY_PARAMETERS` (zero-arg).
2. Else `_schema_from_model(args)` strips `title`, `$defs`; collapses `Optional` `anyOf` to a single non-null type; drops `default: None` keys.
3. Stores `Tool(func, schema={"type":"function","function":{"name","description","parameters"}})` in `TOOLS[func.__name__]`.

The function `__name__` is the registry key — also what providers see as the tool name. Don't shadow.

## Schema shape (consumed by providers)

```python
{
  "type": "function",
  "function": {
    "name": "weather",
    "description": "...",
    "parameters": {
      "type": "object",
      "properties": {
        "location": {"type": "string", "description": "..."},
      },
      "required": ["location"],
    },
  },
}
```

OpenAI/Ollama consume this directly. Anthropic provider translates via `_to_anthropic_tools` (`providers/anthropic_provider.py:9`) into `{name, description, input_schema}`.

## Catalog

| Name | File:line | Args | Returns | Notes |
|---|---|---|---|---|
| `roll_die` | `tools.py:18` | `sides: int` | `str` | RNG. |
| `weather` | `tools.py:94` | `location: str` | `dict` | Geocode via Nominatim → MET Norway forecast. Today + tomorrow summaries. |
| `train_departures` | `tools.py:175` | `station: str, line?: str, count: int=5` | `dict` | Digitraffic Finnish rail. Filters by commuter line letter. |
| `list_emails` | `tools.py:346` | `mailbox="inbox", unread_only=False, from_search?, limit?` | `dict` | Fastmail JMAP. `mailbox="all"` sweeps everywhere except trash/junk/drafts/sent. |
| `read_email` | `tools.py:494` | `email_id: str` | `dict` | Body + attachments. Truncates at 20000 chars. HTML→markdown via `markdownify`. |
| `set_user_preference` | `tools.py:618` | `key, value` | `dict` | → `memory.set_preference`. |
| `add_user_fact` | `tools.py:635` | `text` | `dict` | → `memory.add_fact`. |
| `add_user_instruction` | `tools.py:658` | `text` | `dict` | → `memory.add_instruction`. Behavioural rule. |
| `forget_user_memory` | `tools.py:678` | `matcher` | `dict` | → `memory.forget`. Returns `{status: removed/ambiguous/not_found, ...}`. |
| `get_user_memory` | `tools.py:700` | none | `{markdown: str}` | Full memory file. |
| `search_vault` | `tools.py:725` | `query, max_results=20, context_lines=1` | `dict` | ripgrep wrapper via `vault_search.search`. |
| `read_note` | `tools.py:748` | `path` | `dict` | Vault note read via `vault_search.read`. |
| `escalate_to_big_model` | `tools.py:768` | `query, reason` | `dict` | Calls big model with current session snapshot. Returns `{status, model, reason, reply}`. Disabled if `cfg.escalation_enabled=False`. |

## `_MEMORY_MUTATORS` set

`chat_loop.py:96` — must include any tool that mutates `memory.md`:
```python
{"set_user_preference", "add_user_fact", "add_user_instruction", "forget_user_memory"}
```
Adding a new memory-mutating tool? Update this set, else mid-loop system rebuild won't fire.

## Tool-result format

`_run_tool` (`chat_loop.py:123`):
- Catches all `Exception` and returns `f"Error: {e}"` as the result.
- Otherwise returns the function's return value (any JSON-serializable shape).

`_chat_with_provider` then serializes via `json.dumps(result, ensure_ascii=False)` before passing to `provider.format_tool_results`.

## Slash command wiring (`frontends/common.py`)

Each tool that wants a slash command needs:
1. **Parser**: `parse_<name>_args(args: str) -> dict` (uses `argparse` via `_ArgParser` that raises `ValueError`, or manual parse for free-form).
2. **Formatter**: `format_<name>_result(result: dict) -> str` — markdown-friendly for Discord/CLI; HTML conversion happens automatically for Telegram.
3. **Registry entry**: `TOOL_COMMANDS["<slash>"] = ("<tool_registry_name>", parser, formatter)`.

Currently: `train`, `weather`, `mail`, `search`. CLI wires via `_make_tool_handler` (`cli.py:87`); Telegram/Discord dispatch via `cmd in common.TOOL_COMMANDS` branch.

## Adding a new tool

1. Define pydantic args model (or skip for zero-arg) in `tools.py`.
2. Define function decorated with `@tool(description="...", args=Model)`. Keep description model-friendly — it's the prompt that decides when the tool fires.
3. If memory-mutating: add name to `_MEMORY_MUTATORS` in `chat_loop.py`.
4. Optional slash command: add parser + formatter + `TOOL_COMMANDS` entry in `frontends/common.py`. Update each frontend's command list (Telegram bot menu, CLI `_COMMANDS` dict, help line in `common.HELP_LINES`).
5. Test: `tests/test_tools_registration.py` already verifies registration.
6. Test: `tests/test_routing.py` for slash dispatch.

## httpx client lifetime

`_HTTP = httpx.Client(headers={...}, timeout=15.0)` at module level in `tools/_http.py`. Shared across all tool modules. Connection pool persists for daemon process. Don't add a `close()` — recreating per-call doubles latency.

## Fastmail session caching

`_FM_SESSION` and `_FM_MAILBOXES` are lazily populated module-level globals. Reset them by restarting the bot (no invalidation API). If Fastmail rotates the token, restart.

## Escalation tool details

`escalate_to_big_model(query, reason)`:
1. Disabled if `cfg.escalation_enabled=False` → returns `{status: disabled}`.
2. Reads `chat_loop._active_session_messages.get()` (ContextVar) for full conversation snapshot.
3. Builds `sub_messages = list(snapshot) + [{"role":"user","content": f"[escalation: {reason}] {query}"}]`.
4. Calls `chat_loop._chat_with_provider(big, sub_messages, exclude_tools={"escalate_to_big_model"})` — recursion-prevented.
5. Returns `{"status":"ok","model":"<provider>:<model>","reason","reply"}`.

The local model sees `reply` as a tool result and produces user-facing text. The escalation reply is **not** appended to the active session history — only the synthesized response is.
