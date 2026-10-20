# Feature ideas

Ideas below are based on what's already in the repo plus what I know about your setup from vault memory (homelab, Jellyfin, hovimestari, hermes, and so on).

Legend: `[x]` done, `[~]` partially done, `[ ]` not started. Status last reviewed 2026-10-20.

Standard agent features

Missing core pieces

- [ ] Calendar. Fastmail supports CalDAV. With mail, Wilma and trains already in, agenda queries like "what's tomorrow, do I need to leave early" become possible. hovimestari already has calendar code, but it's in Go, so the logic could be ported rather than shared.
- [ ] Web fetch + search. Read a URL into markdown (defuddle, or plain httpx + readability). Add search through a self-hosted SearXNG, or Kagi/Brave. The local model gets much more useful with these.
- [~] Vault writes. Today it can only search and read notes. Add appending to the daily journal note, creating notes in an inbox folder, and adding tasks. "Remember to X" would then land in Obsidian, not just in memory.md. *Done: `append_to_inbox` appends to `INBOX.md` in the vault root. Not done: daily journal note, creating notes, tasks.*
- [~] Reminders as a first-class feature. The heartbeat at action already works. A thin /remind 15:00 call X slash command on top would cover it, with no new machinery. *Done: the model schedules one-shot `at` actions via `create_heartbeat_action`, and the system prompt tells it to use that for reminders. Not done: a `/remind` slash command that bypasses the model.*

Conversation quality

- [ ] Session summarisation / compaction. Long Telegram/Discord sessions grow without limit, now on disk in the `turns` table including full tool results. When a session crosses a threshold, summarise the old turns into one message and write a boundary.
- [x] Episodic memory. Search past conversations. history.db already exists, so a search_history tool over it is cheap. *Done as `search_conversation_history` in `tools/conversation_history.py`.*
- [ ] Image input. Telegram and Discord photos go to the vision model (gemma4 can do vision). Use cases: receipts, school papers, screenshots.
- [ ] Voice messages. Telegram voice goes through whisper.cpp locally to text. Finnish works reasonably well with whisper large-v3.

Ops / safety

- [x] Cost/usage tracking for escalation. litellm returns token counts. Log them per turn and add a /usage command. *Done: `usage` table in history.db, recorded per LLM call, shown by `/usage`.*
- [ ] Tool confirmation gate. Destructive or outward tools (mail labels, future send-email, homelab restarts) ask before running.
- [x] A /status command. Shows provider, model, uptime, heartbeat next-due times and the last errors. *Done: `common.format_status`, includes recent WARNING+ log lines.*

Bespoke connectors

Homelab / media

- [ ] Sonarr/Radarr. "Is the new episode of X out", "add movie Y". Both have simple REST APIs.
- [ ] Jellyfin. Recently added, continue-watching, and a "pick a movie" mode that calls the moviepicker logic or its endpoint.
- [ ] Homelab health. Uptime checks, disk space, container status. As a heartbeat it only alerts on failures, which fits the HEARTBEAT_OK pattern well.
- [ ] qBittorrent/SABnzbd queue. "Did the download finish."

Finnish life

- [ ] Wilma extensions. Schedule, exams, homework and absences, not just messages. A morning heartbeat could cover "kids' day today".
- [ ] Föli/HSL/Nysse (whichever applies). Local bus departures next to the existing VR trains tool. Digitransit is one GraphQL API for all of Finland.
- [ ] FMI weather warnings. The Finnish Meteorological Institute has open data, which is better than generic weather for warnings.
- [ ] Electricity spot price (porssisahko.net / ENTSO-E). "Cheapest 3h window tonight for the sauna/EV/dishwasher." A heartbeat can alert when the price spikes.
- [ ] Package tracking. Posti and Matkahuolto. Could be auto-detected from shipping emails via check_new_mail.
- [ ] Library loans (Finna / local library). Due-date reminders.

Your own projects

- [ ] hermes data. "What did I rate X", "unwatched Letterboxd watchlist items on Jellyfin". This combines two of your systems.
- [ ] feed-forge / HN / Lobsters digest. A daily heartbeat summarises the top items matching your interests (onboarding already collects interests).
- [~] GitHub. Dependabot alerts across your repos (hovimestari has 23 open), failing CI, PR notifications. A weekly digest heartbeat. *Done: GitHub's hosted read-only MCP server (issues, CI runs, repo search) is the first configured `mcp_servers` entry. Not done: Dependabot alerts, PR notifications, a weekly digest heartbeat.*
- [x] playbook-mcp, or MCP generally. Make metalclaw an MCP client. Every MCP server you run (playbook, future ones) would become tools for free. One feature that unlocks many connectors. *Done: `mcp_client.py` (HTTP + stdio transports, include/exclude filters, untrusted-output note in the system prompt). Deferred work listed in `ai-docs/mcp-plan.md`.*

Added 2026-10-20

- [x] Session persistence across restarts. *Done: `turns` table in history.db stores canonical messages as JSON; `Session.load(scope)` rebuilds from the rows after the latest `/new` boundary, `Session.commit` appends only on a successful turn. Frontends keep no conversation state in memory. Compaction is the natural follow-up: the table grows by full tool results.*
- [ ] Heartbeat run history command. `/heartbeat log` showing the last outcomes and errors per action from the `meta` JSON in `heartbeat_state`. Today failures are only visible in the log file.
- [ ] MCP reconnect. A server that is down at startup stays down. A periodic retry from the heartbeat tick would let the GitHub server survive a token or network blip.
- [ ] Mail reply and send. Reading, labelling and Wilma resolution exist, but the bot cannot answer. Pairs with the tool confirmation gate.
- [ ] `/remind <time> <text>` slash command. Creates a one-shot `at` heartbeat action without a model round-trip, defaulting to the current chat as the delivery channel, with an optional explicit target (`telegram-<id>` / `discord-<id>`).
- [ ] Per-tool model routing. Let specific tools or slash commands always use the escalation model, so hard tasks (Wilma summaries in Finnish) skip the local model without typing `/big`.
- [x] Prompt caching for the litellm path. *Done: `litellm_prompt_caching` (default on) puts a cache breakpoint on the stable system-prompt part and on the last tool for Anthropic / Bedrock Claude models; the volatile tail (clock, memory, heartbeat summary) sits after `SYSTEM_CACHE_BOUNDARY`. Cache read/write tokens surface on `Usage` and the DEBUG log; `/usage` does not show them yet.*
- [ ] Obsidian daily note read. Let the bot read today's and yesterday's daily note so the morning brief has real context, even before vault writes land.

Considered and rejected

- Routing local Ollama through litellm (`ollama_chat/`). Would delete one 183-line httpx client but put a 1.3 s import and a translation layer on every local turn, and tie the hot path to litellm's Ollama adapter. Keep the dedicated provider; the canonical message model already gives provider interchangeability.

My picks, ordered by value/effort

1. ~~MCP client support.~~ Done.
2. Fastmail CalDAV calendar. The missing piece of the daily-life loop.
3. Morning brief heartbeat: calendar + Wilma + weather + trains + electricity price in one message. Mostly composes existing tools. Some overlap with hovimestari, so you'd need to decide which one owns briefs.
4. Homelab health heartbeat. Cheap, and alert-only fits the HEARTBEAT_OK design.
5. Vault journal writes. Turns the bot into a capture inbox. (Inbox append exists; journal/daily note still missing.)
