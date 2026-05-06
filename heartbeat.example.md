---
tasks:
  - name: urgent-mail
    interval: 30m
    prompt: >-
      Check the inbox for unread mail using the list_emails tool
      (mailbox=inbox, unread_only=true). Surface only items that look genuinely
      urgent — deadlines, replies awaited, time-sensitive asks. Skip
      newsletters, receipts, marketing. If nothing urgent, reply HEARTBEAT_OK.
  - name: weather
    interval: 6h
    prompt: >-
      If the user's location is known from memory, fetch today's weather with
      the weather tool and surface anything noteworthy (rain incoming, big
      temperature swing, severe conditions). If location unknown or weather
      unremarkable, reply HEARTBEAT_OK.
---

# Heartbeat checklist

Copy this file to `<vault_path>/<memory_subdir>/heartbeat-<scope>.md` where
`<scope>` is `cli` (for the CLI REPL) or `telegram-<chat_id>` (for a Telegram
chat). Each scope gets its own checklist; empty file or no file = opted out.

Free-form notes below the frontmatter are passed verbatim to the agent on
every tick — useful for ongoing context like "I'm on call this week, escalate
PagerDuty alerts."
