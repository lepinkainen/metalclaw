# dataflow

Sequence diagrams for the four hot paths.

## 1. User turn (any frontend)

```
user types "What's the weather in Helsinki?"
        │
        ▼
frontend handler (cli.py / telegram.py / discord.py)
        │
        ├─ _parse_command(text) → None  (not a slash command)
        ├─ _refresh_system_prompt(messages)   # rebuilds messages[0]
        ├─ messages.append({"role":"user","content": text})
        ▼
loop.run_in_executor(None, lambda: chat(messages, on_tool_call=...))
        │
        ▼
chat() → _chat_with_provider(provider, messages)
        │
        ├─ tool_schemas = TOOLS values
        ├─ system, history = _split_system(messages)
        ├─ _active_session_messages.set(messages)
        ▼
loop:
    am = provider.chat_once(history, tool_schemas, system)
    history += am.raw
    if not am.tool_calls:
        messages[:] = [system_msg] + history
        return am.text
    for tc in am.tool_calls:
        result = _run_tool(tc.name, tc.arguments)        # tools.py funcs
        on_tool_call?(...)                                # frontend display hook
        if tc.name in _MEMORY_MUTATORS: memory_dirty = True
    history += provider.format_tool_results(...)
    if memory_dirty: system = build_system_prompt(now)   # memory write visible next iter
        │
        ▼
back to frontend: reply text → render → send
```

## 2. Escalation turn (`/big <q>` or model calls `escalate_to_big_model`)

### Via `/big`:
```
common.run_big(send, typing_ctx, messages, query)
    ├─ _refresh_system_prompt(messages)
    ├─ messages.append({"role":"user","content": query})
    ▼
chat_via_escalation(messages)
    ├─ big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    └─ _chat_with_provider(big, messages, exclude_tools={"escalate_to_big_model"})
    ▼
(same loop as #1, but with the big provider; escalation tool is hidden)
```

### Via model tool call:
```
chat() running on local provider
    ├─ am.tool_calls = [escalate_to_big_model(query, reason)]
    ▼
_run_tool("escalate_to_big_model", {...})
    ▼
tools.escalate_to_big_model(query, reason):
    ├─ if not cfg.escalation_enabled: return {"status":"disabled"}
    ├─ snapshot = bot._active_session_messages.get()    # ContextVar
    ├─ sub_messages = list(snapshot) + [{"role":"user","content": f"[escalation: {reason}] {query}"}]
    ├─ big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    └─ reply = bot._chat_with_provider(big, sub_messages, exclude_tools={"escalate_to_big_model"})
    ▼
returns {"status":"ok","model":"...","reason","reply"} as the tool result
    ▼
local provider sees the tool result and produces final user-facing text
```

## 3. Heartbeat tick

```
asyncio loop in heartbeat.run()
    ▼
run_tick()
    ├─ active hours? if no → return {}
    ├─ state = load_state()                              # JSON
    └─ for scope in discover_scopes():                   # glob heartbeat-*.md
         ├─ hb = parse_heartbeat_file(file_text)
         ├─ due = [t for t in hb.tasks if is_due(...)]
         ├─ if not due and not hb.body: skip
         ├─ messages = _build_heartbeat_messages(scope, hb, due, now, build_system_prompt)
         │       ▲ system prompt + HEARTBEAT MODE postscript
         ├─ reply = await loop.run_in_executor(None, lambda: bot.chat(messages))
         ├─ state[state_key(scope, t.name)] = now.isoformat()  for each due
         ├─ if reply.strip() startswith SENTINEL: continue     # silent
         └─ channels.for_scope(scope)?.notify(scope, clean)
    ▼
save_state(state)
```

## 4. Memory write + immediate read

```
chat() loop, iteration N:
    am.tool_calls = [add_user_fact(text="user is on call")]
    ▼
_run_tool → memory.add_fact("user is on call")
    ├─ _mutate(apply, "memory write op=add_fact ...")
    │   ├─ acquire _PROCESS_LOCK + fcntl.flock(LOCK_EX)
    │   ├─ mem = _read_locked()
    │   ├─ apply(mem)
    │   ├─ _write_locked(mem)            # atomic temp+replace
    │   │   └─ _invalidate_cache()
    │   └─ release locks
    └─ log.info(...)
    ▼
_chat_with_provider sees tc.name in _MEMORY_MUTATORS:
    system = build_system_prompt(now)    # rebuilds; calls memory.summary()
    ▼
iteration N+1:
    provider.chat_once(history, tools, system)   # model sees new memory in system
```
