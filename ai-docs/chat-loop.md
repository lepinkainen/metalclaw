# chat-loop

`chat_loop.py` — provider-agnostic. No frontend imports.

## Entry points

- `chat(messages, on_tool_call=None) -> str` — uses configured provider.
- `chat_via_escalation(messages) -> str` — uses `cfg.escalation_provider` + `cfg.escalation_model`. Excludes `escalate_to_big_model` from tool list to prevent recursion.

Both mutate `messages` **in place**, appending the provider-native tool-call/result entries plus the final assistant text.

## `_chat_with_provider(provider, messages, *, on_tool_call=None, exclude_tools=frozenset())`

Loop body (`chat_loop.py:147`):

```
1. tool_schemas = [t.schema for name, t in TOOLS.items() if name not in exclude_tools]
2. system, history = _split_system(messages)
3. _active_session_messages.set(messages)  # ContextVar for tools
4. while True:
     am = provider.chat_once(history, tool_schemas, system)
     append am.raw to history (list-or-dict shape)
     if not am.tool_calls:
         messages[:] = ([{"role":"system","content":system}] if system else []) + history
         return am.text
     for each tc in am.tool_calls:
         result = _run_tool(tc.name, tc.arguments)   # catches Exception → "Error: ..."
         result_json = json.dumps(result, ensure_ascii=False)
         on_tool_call?(tc.name, tc.arguments, result_json[:120])
         if tc.name in _MEMORY_MUTATORS: memory_dirty = True
     history.extend(provider.format_tool_results(results))
     if memory_dirty:
         system = build_system_prompt(now)   # rebuild so model sees its own writes
```

Final `messages[:]` reassignment puts the (possibly rebuilt) system message back at index 0.

## System prompt

`build_system_prompt(now)`:
1. `_SYSTEM_PROMPT_BASE.format(now=now)` — date/time + tool-use instructions.
2. If `cfg.escalation_enabled and cfg.provider == "ollama"` → append `_ESCALATION_HINT`. (Cloud providers are already capable; only local models get the hint.)
3. `memory.summary()` if non-empty → append as `"Known about user:\n{summary}"`.

`_refresh_system_prompt(messages)`:
- Regenerates with current time + memory; replaces `messages[0]` if it's a system message, else inserts at 0. Called by every frontend before each user turn (see `cli.py:256`, `telegram.py:217`, `discord.py:243`).

## Mid-loop refresh

When a tool call mutates memory (`_MEMORY_MUTATORS`), the loop rebuilds `system` mid-iteration so the **next** `provider.chat_once` call sees the write. The history is not modified; `system` is passed separately to `chat_once`.

## `_active_session_messages` ContextVar

Set on every `_chat_with_provider` entry; reset on exit. Read by `tools.escalate_to_big_model` (`tools.py:787`) to splice the user's full conversation into the escalation sub-call. Returns `None` outside a chat session — escalation falls back to a single-message context.

## `_parse_command(text)`

Splits leading `/cmd args...` → `(cmd, args)` or returns `None`. No flag parsing here — that's `frontends/common.py`.

## `_split_thinking(text)`

Strips `<think>...</think>` blocks (Gemma/QwQ-style). Returns `(thinking, clean)`. Frontends decide whether to show thinking (CLI: `_show_thinking` global toggled by `/think`; TG/Discord: never shown).

## Escalation

`chat_via_escalation` raises `RuntimeError("escalation is disabled in config")` if `cfg.escalation_enabled` is False. Otherwise:
1. `big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)`.
2. `_chat_with_provider(big, messages, exclude_tools={"escalate_to_big_model"})`.

The `escalate_to_big_model` tool itself (in `tools.py`) does **not** call `chat_via_escalation` — it directly calls `bot._chat_with_provider(big, sub_messages, exclude_tools=...)` with a snapshot of the active session. This avoids appending the escalation result into the active history; the model gets the answer back as a tool result.

## Tool-result formatting per provider

- Ollama: `{"role":"tool", "name": call.name, "content": result_json}` per result.
- OpenAI: `{"role":"tool", "tool_call_id": call.id, "content": result_json}` per result.
- Anthropic: single `{"role":"user", "content": [{"type":"tool_result", "tool_use_id": call.id, "content": result_json}, ...]}`.
