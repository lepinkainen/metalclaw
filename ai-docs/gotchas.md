# gotchas

Subtle traps. Read before editing.

## Tool registration order

`registry.TOOLS` is empty until the `tools` package is imported. `_async_main` does this at `bot.py:33` (`import tools  # noqa: F401`) **before** any frontend starts. Tests must `import tools` to populate. `tests/test_tools_registration.py` enforces.

If you add a new tool module under `tools/`, re-export it from `tools/__init__.py` so it registers on `import tools` — `/add-tool` does this automatically on approve.

## Circular imports

`tools/escalation.py` lazy-imports `chat_loop` + `providers` inside `escalate_to_big_model` (`tools/escalation.py:30`) to avoid the `chat_loop → registry → tools` cycle at module load. `heartbeat.py` lazy-imports `chat_loop` inside `run_tick` (`heartbeat.py:241`) for the same reason. Don't promote either to a top-level import.

## Test imports

Tests import directly from the defining module — `chat_loop._chat_with_provider`, `chat_loop._active_session_messages`, `frontends.discord._split_for_discord`, etc. `bot.py` is a thin entrypoint and re-exports nothing; `import bot; bot._foo` will fail.

## `messages` mutation contract

- `chat()` and `chat_via_escalation()` mutate the passed list in place via `messages[:] = ...` at end.
- Frontends append the user message then call `chat`. On exception they `pop()` to revert.
- The system message at index 0 is **rebuilt** by `_refresh_system_prompt` before each turn AND mid-loop on memory writes — don't rely on `messages[0]` content to be stable across a turn.

## ContextVar `_active_session_messages`

Set by `_chat_with_provider`, read by `escalate_to_big_model` to splice full conversation context into the escalation. **Reset on exit via `try/finally`.** If you call `_chat_with_provider` recursively (e.g. escalation), the inner call sets and resets — outer's snapshot is preserved by `ContextVar.set`/`reset` token semantics. Don't replace with a global mutable.

## Memory mutator detection is name-based

`_MEMORY_MUTATORS` (`chat_loop.py:96`) is a hard-coded frozenset of tool **names**. If you add a memory-mutating tool, add its name here, else mid-loop system rebuild won't fire.

## Anthropic tool-result shape is different

Ollama/OpenAI append one history entry per tool result. **Anthropic appends one `role=user` entry containing all results.** Don't assume `len(format_tool_results(results)) == len(results)`.

## Discord 2000-char split + fences

`_split_for_discord` reopens fenced code blocks across cuts. If you change the splitter, preserve this — splitting mid-fence breaks rendering. Tests in `tests/test_discord.py` cover.

## Discord heartbeat scope vs channel

`discord-<channel_id>` scopes all post to **`cfg.discord_heartbeat_channel`**, not back to the `channel_id` in the scope string. By design — heartbeats post to one shared place. If unset → drop with warning.

## Telegram chat-id persistence

`_known_chats` persists to JSON on every new chat. If the file is deleted, only chats that send a message after restart are eligible for heartbeat fan-out via `active_scopes()` — but `discover_scopes()` (file-based) still works.

## Config caching

`get_config()` is `lru_cache(1)`. Tests must call `config.reset_cache()` after env mutation (`tests/conftest.py:26`). Production never resets — config is immutable per process.

## `extra="ignore"` on Config

Pydantic silently drops unknown yaml fields. Typos in `config.yaml` won't error. Add tests for new fields rather than relying on validation to catch typos.

## Heartbeat YAML parsing alternatives

`_split_yaml` accepts four input shapes (`heartbeat.py:108`). Bare YAML doc requires a top-level `tasks:` key, else falls through to "no frontmatter" and the whole file becomes the body.

## Heartbeat sentinel match is permissive

`clean == SENTINEL or clean.startswith(SENTINEL)` — model output `"HEARTBEAT_OK (nothing urgent)"` is silenced. If you want strict equality, change `heartbeat.py:280` — but the permissive match was deliberate.

## httpx Client lifetime

`tools._HTTP` is module-level — created at import, never closed. Same for `providers/ollama._CLIENT`. Acceptable for daemon process; don't add `_HTTP.close()` cleanup unless you also rebuild on next call.

## Self-change snapshot semantics

`run_self_change` snapshots dirty/untracked files **before** spawning Claude. Reject reverts only the **delta** — pre-existing dirty files are untouched. If you abort mid-run, you must clean up Claude's edits manually because the snapshot is discarded.

## CLI `_cli_messages` is a global

Set by `run_cli_repl` (`cli.py:213`). `/big` reads it via `_cli_messages_ref()`. Only one CLI session at a time — fine for a single-user REPL but not safe for concurrent CLI sessions.

## Provider history is opaque

The chat loop appends `am.raw` and `provider.format_tool_results(...)` outputs to history without inspecting them. **Sessions don't survive a provider switch** — `messages` accumulated under Ollama can't be replayed under Anthropic. If config-flips a provider mid-session, expect breakage.
