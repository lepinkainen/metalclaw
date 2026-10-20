# Metalclaw user manual

This is the canonical user manual for Metalclaw — the bot reads it on demand
to answer questions about its own features. You can edit it freely in
Obsidian; the bot will see your edits next time it answers.

## What is Metalclaw?

Metalclaw is a single-user chatbot that talks to a language-model provider
(Ollama by default, optionally OpenAI or Anthropic) with tool calling. It
runs three frontends in parallel from one process:

- **CLI** — interactive REPL in your terminal.
- **Telegram** — direct messages and groups, replies as Telegram-flavoured HTML.
- **Discord** — DMs and configured guild channels, replies as CommonMark.

Long-term memory is shared across all three frontends via a single markdown
file in your vault. Conversations (short-term context) are kept per-channel.

## Slash commands

Slash commands work in every frontend. They're grouped roughly by purpose:

- **Memory** — `/remember`, `/forget`, `/memory`. Save, remove, and inspect
  the bot's long-term memory of you.
- **Vault** — `/search` reads your Obsidian notes via ripgrep. `/manual`
  reads this manual.
- **Tools** — `/train`, `/weather`, `/mail` invoke their respective tools
  directly without going through the model. Useful when you know exactly
  what you want.
- **Self-modification** — `/add-tool` writes a new tool live in any
  frontend; `/self-edit` (CLI only) makes broader code changes. Both go
  through `/approve`, `/approve_force`, `/reject`, `/diff`.
- **Control** — `/big` routes one turn through the cloud escalation
  provider. `/heartbeat` shows scheduled-task config or fires a tick.
  `/new` resets the conversation. `/help` lists everything. `/manual`
  shows this manual.

The complete, always-current list lives in the **Slash command reference**
section at the bottom of this manual — that section is auto-generated from
the registered commands so it never goes stale.

## Memory system

Long-term memory is a single Obsidian-flavoured markdown file at
`<vault>/<memory_subdir>/memory.md` (default subdir: `Metalclaw/Memory`).
It has three sections:

- `## Preferences` — `- **key**: value` entries. Stable facts about how
  you want to be addressed: role, tone, timezone, interests.
- `## Facts` — free-form bullets. One-off facts that don't fit a
  key/value shape.
- `## Instructions` — durable behavioural rules the bot must follow on
  every turn (e.g. "Always reply in Finnish unless I write in English.").

A short summary of memory is injected into the system prompt at the start
of every turn, and refreshed mid-turn whenever the bot writes to memory —
so the bot sees its own writes immediately.

The bot manages memory with five tools:

- `set_user_preference(key, value)` — save a key/value preference.
- `add_user_fact(text)` — append a free-form fact.
- `add_user_instruction(text)` — save a durable behavioural rule.
- `forget_user_memory(matcher)` — delete by case-insensitive substring;
  refuses on ambiguous matches and lists the candidates.
- `get_user_memory()` — read the full file when the summary is truncated.

You can also drive memory directly:

- `/remember <key>=<value>` — save a preference.
- `/forget <substring>` — remove an entry. Refuses on ambiguous matches.
- `/memory` — show the full memory file.

You can edit `memory.md` by hand in Obsidian; `[[wikilinks]]` are
preserved.

## Heartbeat

Heartbeat is a scheduled, proactive check. The bot can ping you on its
own when something you asked for is due.

You never edit a file for this: ask in chat ("remind me at 16:00 to call
the dentist", "every weekday at 07:00 summarise overnight email", "check
for urgent mail every 30 minutes") and the model records an action in a
bot-owned ledger at `<vault>/<memory_subdir>/heartbeat.yaml` using its
`create_heartbeat_action` tool. Three kinds exist:

- `at` — one-shot at a timestamp. Naive times are your local time.
- `cron` — selected weekdays at one local time in an IANA timezone.
- `every` — a fixed interval in seconds.

Each action remembers which chat created it (`cli`, `telegram-<chat_id>`
or `discord-<channel_id>`) and reports back there; Discord alerts post to
the channel set by `discord_heartbeat_channel` in config. Ask the model to
list or cancel actions in plain language (`list_heartbeat_actions`,
`cancel_heartbeat_action`). Last-run timestamps and failure counters live in the `heartbeat_state`
table of `$XDG_DATA_HOME/metalclaw/history.db`; its `meta` column keeps a
JSON note of what each action last did, handy when debugging.

If a check has nothing to report, the model emits the literal string
`HEARTBEAT_OK` and the bot stays quiet instead of spamming you. One-shot
actions are archived only after the alert was actually delivered. A
failed run or delivery keeps the action active and retries with
exponential backoff (next tick, then 2x, 4x… up to an hour) so a
misconfigured channel does not burn a model call every tick; `/status`
shows actions that are backing off.

`heartbeat_active_hours` in config (e.g. `[8, 22]`) limits when ticks
can fire.

Slash commands:

- `/heartbeat` — show config, active actions and recent completed ones.
- `/heartbeat run` — fire a tick immediately.

## Escalation

When `escalation_enabled: true` in `config.yaml`, the local model can
delegate hard questions to a bigger cloud model — and you can bypass the
local model entirely with `/big <query>`.

Escalation goes through the provider configured by `escalation_provider`
(`litellm` for any cloud model, or `ollama`) using `escalation_model`,
which defaults to that provider's configured model. The local model
calls the `escalate_to_big_model` tool on its own when a question is
beyond its capability (deep code analysis, niche knowledge, complex
reasoning). It does not escalate trivial requests.

Slash command:

- `/big <query>` — run one turn through the escalation provider directly.
  Useful when you already know you want the cloud model.

## Self-modification

Metalclaw can modify its own source. Two paths:

- **`/add-tool <description>`** — live, available in CLI / Telegram /
  Discord. Spawns `claude -p` with a constrained contract: write exactly
  one new file at `tools/<slug>.py`, edit nothing else. Focused gates run
  in seconds: ruff on the new file, import + register sanity check,
  schema-shape check. The new tool is callable in the *current* session
  immediately because importing the new module fires the `@tool`
  decorator. On `/approve`, the import is also persisted to
  `tools/__init__.py` so the tool survives restarts.

- **`/self-edit <description>`** — CLI only, restart-tied. Spawns
  `claude -p` with broader permissions; runs full `task lint/build/test`
  gates. Approved changes take effect on the next bot launch.

Both paths converge on the same approval slash commands:

- `/approve` — accept if all gates passed.
- `/approve_force` (or `/approve-force` in CLI) — accept regardless.
- `/reject` — discard. For `/add-tool`, unlinks the new file and pops
  the new keys from `registry.TOOLS`. For `/self-edit`, runs
  `git checkout --` on tracked changes and unlinks new untracked files.
- `/diff` — show the diff; the pending change stays in place.

Approved entries are appended to `changes.jsonl` at the repo root.

## Frontends

The three frontends share the chat loop and memory but differ in
rendering and routing:

- **CLI** — `prompt_toolkit` input loop with slash-command completion.
  Replies are rendered with Rich (`rich.markdown.Markdown`). Toggle
  thinking display with `/think`. `/self-edit` is CLI-only.

- **Telegram** — per-`chat_id` short-term session. Replies are
  CommonMark converted to Telegram-flavoured HTML via `markdown-it-py`.
  Slash commands are registered as Telegram bot commands so they appear
  in the in-app menu. The bot replies to every direct message and group
  message it sees.

- **Discord** — per-`channel.id` short-term session. Replies are sent
  as raw CommonMark (Discord renders it natively) and split at 2000
  chars, reopening fenced code blocks across cuts. The bot replies to
  every DM, every message in a channel listed in `discord_chat_channels`,
  and any message in any channel that mentions it or replies to one of
  its own messages. Heartbeats addressed to any `discord-…` scope post
  to the single channel set in `discord_heartbeat_channel`.

## Configuration

Config lives in `config.yaml`. Search order:

1. `METALCLAW_CONFIG` env var (if set).
2. `./config.yaml` in the current working directory.
3. `$XDG_CONFIG_HOME/metalclaw/config.yaml` (or `~/.config/metalclaw/config.yaml`).

See `config.example.yaml` in the repo root for the full schema with
inline comments — every field is documented there. Highlights:

- `vault_path` (required) — your Obsidian vault root.
- `memory_subdir` — subdirectory inside the vault for `memory.md` and
  heartbeat scope files. Default: `Metalclaw/Memory`.
- `provider` — `ollama`, `openai`, or `anthropic`.
- `escalation_*` — opt-in cloud fallback.
- `heartbeat_*` — schedule and active-hours window.

Environment overrides: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`FASTMAIL_API_TOKEN`, `OLLAMA_URL`, `TELEGRAM_BOT_TOKEN`,
`DISCORD_BOT_TOKEN`. Each takes precedence over the matching
`config.yaml` field.

## Tools reference

(This section is filled in at read time with the live list of registered
tools — name and description for each. If you're reading the raw template
file in your vault, this section will look empty.)

## Slash command reference

(This section is filled in at read time with the live list of slash
commands across all three frontends. If you're reading the raw template
file in your vault, this section will look empty.)
