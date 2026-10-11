# metalclaw — Project Purpose

## What it is

A personal LLM assistant with a persistent memory file and multiple chat front ends — CLI, Telegram and Discord — over a local-first model backend with optional cloud providers. It runs tools, keeps notes about you across conversations, and wakes itself on a schedule to do things unprompted.

The two distinguishing pieces are the **heartbeat** (scheduled actions the assistant manages itself, letting it act without being asked) and **self-change** (the assistant editing its own code under user approval, tracked in `changes.jsonl`).

## Current capabilities

- Local-first chat with cloud providers as an option, behind a common provider interface
- Tool registry with slash-command wiring; tools under `tools/`
- Persistent memory with atomic writes, locking and explicit forget semantics (`memory.py`)
- Vault search over the Obsidian vault (`vault_search.py`)
- Front ends: CLI (prompt_toolkit), Telegram (python-telegram-bot), Discord (discord.py), with per-frontend session and gating differences
- Heartbeat scheduler running assistant-managed actions, staying quiet when there is nothing to report
- Escalation from the local model to a bigger cloud model for harder turns
- Docker/compose deployment; config via YAML with environment overrides and validators

## Direction

Architecture is documented far more thoroughly than usual — `ai-docs/` holds dense, indexed, file:line-referenced notes written for AI consumption. **Start at `ai-docs/INDEX.md`** and load the topic file matching the area being edited.

## Constraints

- Python 3.14, uv-managed.
- Personal single-user assistant with access to private data (memory, Obsidian vault, messaging accounts) — credentials via environment/config, never the repo; gitleaks config present for a reason.
- Self-change is powerful and logged to `changes.jsonl`; keep that audit trail intact.
- Known sharp edges (circular imports, ContextVar handling) are catalogued in `ai-docs/gotchas.md`.
