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
        ├─ run_turn(session, text, ...):
        │     _refresh_system_prompt(session)   # rebuilds session.system
        │     session.append_user(text)
        ▼
loop.run_in_executor(None, lambda: chat(session, on_tool_call=...))
        │
        ▼
chat() → _check_session_provider(session) → _chat_with_provider(provider, session)
        │
        ├─ tool_schemas = TOOLS values
        ├─ system = session.system; history = list(session.history)   # work on a copy
        ├─ _active_session.set(Session(system, list(history)))
        ▼
loop:
    _active_session.set(Session(system, list(history)))   # republish each round
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
common.run_big(send, typing_ctx, session, query)
    ├─ run_turn: _refresh_system_prompt(session); session.append_user(query)
    ▼
chat_via_escalation(session)
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
    ├─ snapshot = chat_loop._active_session.get()    # ContextVar, Session | None
    ├─ sub = snapshot.snapshot(); sub.append_user(f"[escalation: {reason}] {query}")
    ├─ big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    └─ reply = chat_loop._chat_with_provider(big, sub_messages, exclude_tools={"escalate_to_big_model"})
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
    ├─ state = load_state()                              # {id: ActionState} from history.db
    ├─ ledger = load_ledger()                            # heartbeat.yaml under flock
    └─ for action in ledger.actions:
         ├─ if not is_action_due(action, state.last_run.get(id), now): skip
         ├─ if now < state[id].retry_at(): skip           # backoff after failures, no model call
         ├─ session = _build_action_session(action, now, build_system_prompt)
         │       ▲ system prompt (format_now(now) clock) + HEARTBEAT MODE postscript
         ├─ reply = await _run_action(...)                # chat_loop.chat in executor
         │       on exception → mark_failure (row upsert), continue
         ├─ clean = reply.strip()
         ├─ if "" and no SENTINEL → mark_failure, continue
         ├─ if SENTINEL in clean (≤300 leftover chars) → mark_success; at → archive; continue
         ├─ channel = _resolve_channel(action.channel or cfg.heartbeat_default_channel)
         │       None → mark_failure, continue
         ├─ await channel.notify(scope, clean)           # raises on non-delivery → mark_failure
         └─ mark_success(id, now); kind=at → _archive_with_lock(id, now)
    ▼
prune_state(active ∪ completed ids)                   # drop orphan rows; no bulk save
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
