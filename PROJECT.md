# metalclaw — Project Purpose

## What it is

A personal LLM assistant with a persistent memory file and two chat front ends — Telegram and Discord, run headless from one `bot.py` process together with the heartbeat scheduler — over a local-first model backend with optional cloud providers. It runs tools, keeps notes about you across conversations, and wakes itself on a schedule to do things unprompted.

The two distinguishing pieces are the **heartbeat** (scheduled actions the assistant manages itself, letting it act without being asked) and **self-change** (the assistant writing new tool modules via `/add-tool` under user approval, opt-in via `allow_self_modification`, tracked in `changes.jsonl`).

## Current capabilities

- Local-first chat (Ollama) with cloud providers via litellm as an option, behind a common provider interface
- Tool registry with slash-command wiring; tools under `tools/`
- Persistent memory with atomic writes, locking and explicit forget semantics (`memory.py`)
- Vault search over the Obsidian vault (`vault_search.py`)
- Front ends: Telegram (python-telegram-bot) and Discord (discord.py), with sender allowlists and per-frontend gating differences; conversations are persisted per chat in the `turns` table of `history.db` and survive restarts
- Heartbeat scheduler running assistant-managed actions, staying quiet when there is nothing to report
- Watcher pipelines (`pipelines/`, mail first): cheap polling and deterministic filters, calling the model only when something is left
- MCP client (`mcp_client.py`) exposing tools from configured MCP servers, results marked untrusted
- Wilma (Finnish school portal) messages and news (`wilma.py`)
- Escalation from the local model to a bigger cloud model for harder turns
- Docker/compose deployment; config via YAML with environment overrides and validators

## Direction

Architecture is documented far more thoroughly than usual — `ai-docs/` holds dense, indexed, file:line-referenced notes written for AI consumption. **Start at `ai-docs/INDEX.md`** and load the topic file matching the area being edited.

## Constraints

- Python 3.14, uv-managed.
- Personal single-user assistant with access to private data (memory, Obsidian vault, messaging accounts) — credentials via environment/config, never the repo; gitleaks config present for a reason.
- Self-change is powerful and logged to `changes.jsonl`; keep that audit trail intact.
- Known sharp edges (circular imports, ContextVar handling) are catalogued in `ai-docs/gotchas.md`.
