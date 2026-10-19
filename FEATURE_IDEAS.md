# Feature ideas

Ideas below are based on what's already in the repo plus what I know about your setup from vault memory (homelab, Jellyfin, hovimestari, hermes, and so on).

Standard agent features

Missing core pieces

- Calendar. Fastmail supports CalDAV. With mail, Wilma and trains already in, agenda queries like "what's tomorrow, do I need to leave early" become possible. hovimestari already has calendar code, but it's in Go, so the logic could be ported rather than shared.
- Web fetch + search. Read a URL into markdown (defuddle, or plain httpx + readability). Add search through a self-hosted SearXNG, or Kagi/Brave. The local model gets much more useful with these.
- Vault writes. Today it can only search and read notes. Add appending to the daily journal note, creating notes in an inbox folder, and adding tasks. "Remember to X" would then land in Obsidian, not just in memory.md.
- Reminders as a first-class feature. The heartbeat at action already works. A thin /remind 15:00 call X slash command on top would cover it, with no new machinery.

Conversation quality

- Session summarisation / compaction. Long Telegram/Discord sessions grow without limit. When a session crosses a threshold, summarise the old turns into one message.
- Episodic memory. Search past conversations. history.db already exists, so a search_history tool over it is cheap.
- Image input. Telegram and Discord photos go to the vision model (gemma4 can do vision). Use cases: receipts, school papers, screenshots.
- Voice messages. Telegram voice goes through whisper.cpp locally to text. Finnish works reasonably well with whisper large-v3.

Ops / safety

- Cost/usage tracking for escalation. litellm returns token counts. Log them per turn and add a /usage command.
- Tool confirmation gate. Destructive or outward tools (mail labels, future send-email, homelab restarts) ask before running.
- A /status command. Shows provider, model, uptime, heartbeat next-due times and the last errors.

Bespoke connectors

Homelab / media

- Sonarr/Radarr. "Is the new episode of X out", "add movie Y". Both have simple REST APIs.
- Jellyfin. Recently added, continue-watching, and a "pick a movie" mode that calls the moviepicker logic or its endpoint.
- Homelab health. Uptime checks, disk space, container status. As a heartbeat it only alerts on failures, which fits the HEARTBEAT_OK pattern well.
- qBittorrent/SABnzbd queue. "Did the download finish."

Finnish life

- Wilma extensions. Schedule, exams, homework and absences, not just messages. A morning heartbeat could cover "kids' day today".
- Föli/HSL/Nysse (whichever applies). Local bus departures next to the existing VR trains tool. Digitransit is one GraphQL API for all of Finland.
- FMI weather warnings. The Finnish Meteorological Institute has open data, which is better than generic weather for warnings.
- Electricity spot price (porssisahko.net / ENTSO-E). "Cheapest 3h window tonight for the sauna/EV/dishwasher." A heartbeat can alert when the price spikes.
- Package tracking. Posti and Matkahuolto. Could be auto-detected from shipping emails via check_new_mail.
- Library loans (Finna / local library). Due-date reminders.

Your own projects

- hermes data. "What did I rate X", "unwatched Letterboxd watchlist items on Jellyfin". This combines two of your systems.
- feed-forge / HN / Lobsters digest. A daily heartbeat summarises the top items matching your interests (onboarding already collects interests).
- GitHub. Dependabot alerts across your repos (hovimestari has 23 open), failing CI, PR notifications. A weekly digest heartbeat.
- playbook-mcp, or MCP generally. Make metalclaw an MCP client. Every MCP server you run (playbook, future ones) would become tools for free. One feature that unlocks many connectors.

My picks, ordered by value/effort

1. MCP client support. One feature, many connectors, and it reuses the servers you already run.
2. Fastmail CalDAV calendar. The missing piece of the daily-life loop.
3. Morning brief heartbeat: calendar + Wilma + weather + trains + electricity price in one message. Mostly composes existing tools. Some overlap with hovimestari, so you'd need to decide which one owns briefs.
4. Homelab health heartbeat. Cheap, and alert-only fits the HEARTBEAT_OK design.
5. Vault journal writes. Turns the bot into a capture inbox.

My picks, ordered by value/effort

1. MCP client support. One feature, many connectors, and it reuses the servers you already run.
2. Fastmail CalDAV calendar. The missing piece of the daily-life loop.
3. Morning brief heartbeat: calendar + Wilma + weather + trains + electricity price in one message. Mostly composes existing tools. Some overlap with hovimestari, so you'd need to decide which one owns briefs.
4. Homelab health heartbeat. Cheap, and alert-only fits the HEARTBEAT_OK design.
5. Vault journal writes. Turns the bot into a capture inbox.

Want any of these dug into: feasibility, API check, or a plan?
