import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

import wilma
from registry import tool

log = logging.getLogger(__name__)


class _ListWilmaMessagesArgs(BaseModel):
    student: str | None = Field(
        default=None,
        description="Child's name (or part of it) or Wilma student number. Omit to cover all children.",
    )
    folder: Literal["inbox", "archive", "outbox"] = Field(
        default="inbox", description="Wilma message folder."
    )
    unread_only: bool = Field(
        default=False, description="Only messages not yet opened in Wilma."
    )
    limit: int = Field(
        default=20, ge=1, le=100, description="Max messages per child, newest first."
    )


@tool(
    description=(
        "List messages (Viestit) from Wilma, the school information system: subject, sender, sent time, "
        "unread flag and id, per child. Use read_wilma_message with the id and student to get the full text."
    ),
    args=_ListWilmaMessagesArgs,
)
def list_wilma_messages(
    student: str | None = None,
    folder: str = "inbox",
    unread_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    client = wilma.get_client()
    out = []
    for s in wilma.resolve_students(client, student):
        msgs = client.list_messages(s["number"], folder)
        if unread_only:
            msgs = [m for m in msgs if m["unread"]]
        msgs.sort(key=lambda m: m["sent_at"] or "", reverse=True)
        out.append(
            {
                "student": s["number"],
                "student_name": s["name"],
                "messages": msgs[:limit],
            }
        )
    log.info("list_wilma_messages folder=%s students=%d", folder, len(out))
    return {"folder": folder, "students": out}


class _ReadWilmaMessageArgs(BaseModel):
    message_id: int = Field(description="Message id from list_wilma_messages.")
    student: str | None = Field(
        default=None,
        description="Student number (or name) the message was listed under. Omit to try each child.",
    )


@tool(
    description="Read one Wilma message in full (body as markdown, recipients, replies).",
    args=_ReadWilmaMessageArgs,
)
def read_wilma_message(message_id: int, student: str | None = None) -> dict[str, Any]:
    client = wilma.get_client()
    last_error: Exception | None = None
    for s in wilma.resolve_students(client, student):
        try:
            msg = client.get_message(s["number"], message_id)
        except wilma.WilmaAuthError:
            raise
        except wilma.WilmaError as e:
            # Messages are filed per child; another child's prefix may be the right one.
            log.info(
                "read_wilma_message id=%s not under student=%s: %s",
                message_id,
                s["number"],
                e,
            )
            last_error = e
            continue
        return {"student": s["number"], "student_name": s["name"], **msg}
    raise last_error or ValueError(f"Wilma message {message_id} not found")


class _FindWilmaMessageArgs(BaseModel):
    subject: str = Field(
        description=(
            "Subject from the Wilma notification email, e.g. 'Viesti Wilmasta: Pidä huolta! -koulupoliisikonsertit Jä'. "
            "The 'Viesti Wilmasta:' prefix and truncation are handled."
        )
    )
    sent_at: str | None = Field(
        default=None,
        description="Send time from the notification, e.g. '19.10.2026 16:08' or ISO. Narrows ambiguous matches.",
    )
    student: str | None = Field(
        default=None, description="Child's name or student number, if known."
    )


@tool(
    description=(
        "Find the Wilma message behind a Wilma notification email (sender *@inschool.fi, subject "
        "'Viesti Wilmasta: …', body only says there is a new message) and return its full content. "
        "Use this when reporting such emails instead of telling the user to open Wilma."
    ),
    args=_FindWilmaMessageArgs,
)
def find_wilma_message(
    subject: str, sent_at: str | None = None, student: str | None = None
) -> dict[str, Any]:
    client = wilma.get_client()
    candidates = []
    for s in wilma.resolve_students(client, student):
        for m in wilma.match_messages(
            client.list_messages(s["number"]), subject, sent_at
        ):
            candidates.append((s, m))
    # A message sent to every guardian shows up once per child — same content, keep one.
    unique = {(m["subject"], m["sent_at"], m["sender"]): (s, m) for s, m in candidates}
    log.info("find_wilma_message candidates=%d unique=%d", len(candidates), len(unique))
    if not unique:
        return {"status": "not_found", "subject": subject, "sent_at": sent_at}
    if len(unique) > 1:
        return {
            "status": "ambiguous",
            "candidates": [
                {"student": s["number"], "student_name": s["name"], **m}
                for s, m in unique.values()
            ],
        }
    s, m = next(iter(unique.values()))
    students = sorted({c[0]["name"] or c[0]["number"] or "" for c in candidates} - {""})
    msg = client.get_message(s["number"], m["id"])
    return {"status": "found", "student": s["number"], "students": students, **msg}


class _ListWilmaNewsArgs(BaseModel):
    student: str | None = Field(
        default=None,
        description="Child's name (or part of it) or Wilma student number. Omit to cover all children.",
    )
    since: str | None = Field(
        default=None,
        description=(
            "Only bulletins published on or after this date ('2026-10-19' or '19.10.2026'). "
            "Pinned bulletins are always included."
        ),
    )
    include_older: bool = Field(
        default=False,
        description="Also list the archived 'Vanhat tiedotteet' (titles only until read).",
    )
    limit: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Max dated bulletins per child, newest first.",
    )


@tool(
    description=(
        "List school bulletins (Tiedotteet) from Wilma: id, title, published date, author, pinned flag, "
        "per child. Bulletins are announcements to all guardians, separate from messages (Viestit). "
        "Use this when a Wilma email says 'Uusia tiedotteita' (new bulletins); then read_wilma_news "
        "for the full text."
    ),
    args=_ListWilmaNewsArgs,
)
def list_wilma_news(
    student: str | None = None,
    since: str | None = None,
    include_older: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    client = wilma.get_client()
    cutoff = wilma.parse_timestamp(since) if since else None
    if since and cutoff is None:
        raise ValueError(f"could not parse since={since!r}; use YYYY-MM-DD")
    out = []
    for s in wilma.resolve_students(client, student):
        items = client.list_news(s["number"])
        dated = [i for i in items if not i["archived"] and i["published"]]
        dated.sort(key=lambda i: i["published"], reverse=True)
        if cutoff:
            dated = [
                i
                for i in dated
                if i["pinned"]
                or (wilma.parse_timestamp(i["published"]) or cutoff) >= cutoff
            ]
        undated = [i for i in items if not i["archived"] and not i["published"]]
        older = [i for i in items if i["archived"]] if include_older else []
        out.append(
            {
                "student": s["number"],
                "student_name": s["name"],
                "news": dated[:limit] + undated + older,
            }
        )
    log.info("list_wilma_news students=%d since=%s", len(out), since)
    return {"students": out}


class _ReadWilmaNewsArgs(BaseModel):
    news_id: int = Field(description="Bulletin id from list_wilma_news.")
    student: str | None = Field(
        default=None,
        description="Student number (or name) the bulletin was listed under. Omit to try each child.",
    )


@tool(
    description=(
        "Read one Wilma bulletin (tiedote) in full: body as markdown plus the links it carries "
        "(attachments are usually links)."
    ),
    args=_ReadWilmaNewsArgs,
)
def read_wilma_news(news_id: int, student: str | None = None) -> dict[str, Any]:
    client = wilma.get_client()
    last_error: Exception | None = None
    for s in wilma.resolve_students(client, student):
        try:
            item = client.get_news(s["number"], news_id)
        except wilma.WilmaAuthError:
            raise
        except wilma.WilmaError as e:
            log.info(
                "read_wilma_news id=%s not under student=%s: %s",
                news_id,
                s["number"],
                e,
            )
            last_error = e
            continue
        return {"student": s["number"], "student_name": s["name"], **item}
    raise last_error or ValueError(f"Wilma bulletin {news_id} not found")
