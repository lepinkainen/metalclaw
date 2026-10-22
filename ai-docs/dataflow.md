# dataflow

Sequence diagrams for the four hot paths.

## 1. User turn (Telegram / Discord)

```
user types "What's the weather in Helsinki?"
        │
        ▼
frontend handler (telegram.py / discord.py)
        │
        ├─ _parse_command(text) → None  (not a slash command)
        ├─ Debouncer: quiet period → _run_text_turn(scope, joined_text)
        ├─ async with <per-chat lock>:
        │     session = await asyncio.to_thread(Session.load, scope)
        │           ├─ history_db.turns_load(scope)      # rows after latest /new boundary → list[Message]
        │           └─ system = build_system_prompt(format_now()); _persisted = len(history)
        │     run_turn(session, text, lambda: scoped_chat(scope, lambda: chat(session, on_tool_call=...))):
        │           session.append_user(text)           # in memory only
        ▼
loop.run_in_executor(None, _work):   # _refresh_system_prompt(session); chat(...)
        │
        ▼
chat() → _chat_with_provider(get_provider(cfg.provider), session)
        │
        ├─ tool_schemas = TOOLS values
        ├─ work = session.snapshot()                       # scope-less copy
        ├─ _active_session.set(work.snapshot())
        ▼
loop:
    _active_session.set(work.snapshot())                  # republish each round
    am = provider.chat_once(work.history, tool_schemas, work.system)   # provider._to_wire inside
    if not am.tool_calls:
        work.history.append(am)
        session.commit(work)                              # turns_append(scope, work.history[_persisted:]) THEN adopt
        return am.text
    for tc in am.tool_calls:
        result = _run_tool(tc.name, tc.arguments)        # tools/ funcs
        on_tool_call?(...)                                # frontend display hook
        if tc.name in _MEMORY_MUTATORS: memory_dirty = True
    work.append_exchange(am, [ToolResultMessage(...)])    # validates call ids; not yet persisted
    if memory_dirty: work.system = build_system_prompt(now)   # memory write visible next iter
        │
        ├─ on exception anywhere above: nothing committed, run_turn pops the user msg, turns table unchanged
        ▼
back to frontend: _split_thinking → clean reply → render → send; session object dropped (next turn re-loads)
```

`/new` → `asyncio.to_thread(Session.reset, scope)` → `turns_reset` inserts a `boundary` row; the next `Session.load` sees an empty history.

## 2. Escalation turn (`/big <q>` or model calls `escalate_to_big_model`)

### Via `/big`:
```
frontend: session = await asyncio.to_thread(Session.load, scope)
common.run_big(send, typing_ctx, session, query, scope=scope, on_tool_call=...)
    ├─ run_turn: session.append_user(query); _refresh_system_prompt(session)
    ▼
chat_via_escalation(session)
    ├─ big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    └─ _chat_with_provider(big, session, exclude_tools={"escalate_to_big_model"})
    ▼
(same loop as #1, big provider, escalation tool hidden; session.commit persists the turn to the same scope)
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
    ├─ snapshot = chat_loop._active_session.get()    # ContextVar, Session | None (scope-less copy)
    ├─ sub = snapshot.snapshot(); sub.append_user(f"[escalation: {reason}] {query}")
    ├─ big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    └─ reply = chat_loop._chat_with_provider(big, sub, exclude_tools={"escalate_to_big_model"})
    ▼                                                 # sub.commit is in-memory; sub discarded
returns {"status":"ok","model":"...","reason","reply"} as the tool result (this *is* persisted, as the outer turn's ToolResultMessage)
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
         │       ▲ scope-less: system prompt (format_now(now) clock) + HEARTBEAT MODE postscript; never written to turns
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
_expire_completed_with_lock(now)                      # trim yaml completed: >7d; state rows kept; no bulk save
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
