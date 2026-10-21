"""Code-first mail check: fetch → label filter → ledger → cap → model → deliver.

Only the model stage costs tokens, so the action can run every few minutes.
Design and rationale: ``ai-docs/mail-pipeline-plan.md``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from pipelines.context import Outcome, PipelineContext
from tools import mail

log = logging.getLogger("metalclaw.pipelines.mail")

SOURCE = mail.LEDGER_SOURCE
TOOLS: frozenset[str] = frozenset(
    {"read_email", "find_wilma_message", "add_email_label", "remove_email_label"}
)

SYSTEM = (
    "MAIL CHECK: This is a scheduled, code-driven mail check, not a user message. "
    "The user message lists unread emails that passed the user's own filters and "
    "have not been reported before. Decide what the user needs to know. Use "
    "read_email (or the tool named in an email's `hint`) when the preview is not "
    "enough. Reply with a concise summary for the user, grouped sensibly; skip "
    "mail that needs no attention. If nothing requires the user's attention, reply "
    "with exactly `HEARTBEAT_OK` and nothing else. Do not call any marking tool; "
    "the batch is recorded automatically."
)


def _user_message(emails: list[dict]) -> str:
    rows = [
        {
            k: v
            for k, v in e.items()
            if k
            in {"id", "subject", "from", "received_at", "preview", "folders", "hint"}
        }
        for e in emails
    ]
    return f"{len(rows)} unread email(s) to review (oldest first):\n\n" + json.dumps(
        rows, ensure_ascii=False, indent=1
    )


async def run(ctx: PipelineContext) -> Outcome:
    cfg = ctx.config
    with ctx.trace.step("fetch") as st:
        await asyncio.to_thread(mail.migrate_legacy_ledger)
        heads = await asyncio.to_thread(mail.fetch_unread_heads)
        st.set_note(f"{len(heads)} unread")

    with ctx.trace.step("labels") as st:
        heads, dropped = await asyncio.to_thread(
            mail.drop_labelled, heads, cfg.mail_ignore_labels
        )
        if dropped:
            st.set_note(
                f"dropped {sum(dropped.values())} ("
                + ", ".join(f"{k} {v}" for k, v in sorted(dropped.items()))
                + ")"
            )

    with ctx.trace.step("ledger") as st:
        before = len(heads)
        heads = await asyncio.to_thread(ctx.ledger.drop_reported, SOURCE, heads)
        st.set_note(f"dropped {before - len(heads)}, {len(heads)} left")

    if not heads:
        return Outcome.SUPPRESSED

    with ctx.trace.step("bodies") as st:
        batch = heads[: cfg.mail_pipeline_max_batch]
        emails = await asyncio.to_thread(mail.fetch_bodies, [h["id"] for h in batch])
        emails = mail.attach_handler_hints(emails)
        st.set_note(
            f"{len(emails)} fetched"
            + (
                f", {len(heads) - len(batch)} deferred"
                if len(heads) > len(batch)
                else ""
            )
        )
    if not emails:
        # Every head vanished between the two fetches (deleted/moved); nothing to show.
        return Outcome.SUPPRESSED

    with ctx.trace.step("model") as st:
        reply = await ctx.ask_model(
            system=SYSTEM, user=_user_message(emails), tools=TOOLS
        )
        st.set_note(f"{len(reply)} chars")

    from heartbeat import strip_sentinel  # local: heartbeat imports pipelines

    text, acked = strip_sentinel(reply)
    if acked and not text:
        with ctx.trace.step("mark"):
            await asyncio.to_thread(ctx.ledger.mark, SOURCE, emails, Outcome.DISMISSED)
        return Outcome.DISMISSED
    if not text:
        raise RuntimeError("empty reply without HEARTBEAT_OK")

    with ctx.trace.step("deliver"):
        await ctx.deliver(text)
    with ctx.trace.step("mark"):
        await asyncio.to_thread(ctx.ledger.mark, SOURCE, emails, Outcome.DELIVERED)
    return Outcome.DELIVERED
