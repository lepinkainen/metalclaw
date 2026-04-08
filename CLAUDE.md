# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Metalclaw is a CLI chatbot that uses a local Ollama model (Qwen) with tool-calling capabilities, plus a controlled self-modification system that invokes Claude Code as a subprocess to edit its own source.

## Commands

```
task build    # Import-check all modules (bot, tools, registry, self_change)
task lint     # ruff check .
task test     # pytest -q
```

Run a single test: `uv run pytest tests/test_routing.py::test_help_command -q`

## Architecture

- **registry.py** — `@tool` decorator and global `TOOLS` dict. Every tool auto-registers on import via the decorator.
- **tools.py** — Tool implementations (weather, dice, train departures). Imported at runtime in `bot.main()` to trigger registration.
- **bot.py** — Ollama chat client with tool-call loop, `/command` dispatch, and CLI REPL. Uses `httpx` to talk to `localhost:11434`.
- **self_change.py** — Invokes `claude -p` as a subprocess to implement code changes, then runs lint+test gates and prompts the user to approve/reject. Approved changes are logged to `changes.jsonl`.

### Adding a new tool

Define a function in `tools.py` decorated with `@tool(description=..., parameters=...)`. It auto-registers into `TOOLS` when the module is imported. The schema follows Ollama's tool-calling format (OpenAI-compatible function schema).

### Self-modification flow

`/add-tool` and `/self-edit` commands → `self_change.run_self_change()` → Claude subprocess edits files → lint & test gates → interactive approve/reject → optional JSONL log.
