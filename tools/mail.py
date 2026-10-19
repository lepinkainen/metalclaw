import logging
import sqlite3
import time
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from config import get_config, xdg_data_dir
from registry import tool
from wilma import is_notification_subject

from ._http import HTTP

log = logging.getLogger(__name__)

_FASTMAIL_SESSION_URL = "https://api.fastmail.com/jmap/session"

_FM_SESSION: dict[str, str] | None = None
_FM_MAILBOXES: dict[str, Any] | None = None


def _fm_session() -> dict[str, str]:
    global _FM_SESSION
    if _FM_SESSION is not None:
        log.debug("jmap: session cache hit")
        return _FM_SESSION
    token = get_config().fastmail_api_token
    if not token:
        raise ValueError(
            "Fastmail API token not configured. Set fastmail_api_token in config.yaml "
            "or FASTMAIL_API_TOKEN env var."
        )
    t0 = time.monotonic()
    resp = HTTP.get(_FASTMAIL_SESSION_URL, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    data = resp.json()
    log.info(
        "jmap: session fetched status=%s latency=%.2fs api_url=%s",
        getattr(resp, "status_code", None),
        time.monotonic() - t0,
        data.get("apiUrl"),
    )
    _FM_SESSION = {
        "api_url": data["apiUrl"],
        "account_id": data["primaryAccounts"]["urn:ietf:params:jmap:mail"],
        "token": token,
    }
    return _FM_SESSION


def _fm_mailboxes() -> dict[str, Any]:
    global _FM_MAILBOXES
    if _FM_MAILBOXES is not None:
        log.debug("jmap: mailbox cache hit (%d mailboxes)", len(_FM_MAILBOXES["by_id"]))
        return _FM_MAILBOXES
    session = _fm_session()
    t0 = time.monotonic()
    resp = HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                [
                    "Mailbox/get",
                    {"accountId": session["account_id"], "ids": None},
                    "mb",
                ],
            ],
        },
    )
    resp.raise_for_status()
    mailboxes = resp.json()["methodResponses"][0][1]["list"]
    log.info(
        "jmap: Mailbox/get status=%s mailboxes=%d latency=%.2fs (cache miss)",
        getattr(resp, "status_code", None),
        len(mailboxes),
        time.monotonic() - t0,
    )
    by_role: dict[str, dict[str, Any]] = {}
    by_name: dict[str, dict[str, Any]] = {}
    by_id: dict[str, dict[str, Any]] = {}
    for mb in mailboxes:
        entry = {
            "id": mb["id"],
            "name": mb["name"],
            "role": mb.get("role"),
            "total": mb.get("totalEmails", 0),
            "unread": mb.get("unreadEmails", 0),
            "parentId": mb.get("parentId"),
        }
        if mb.get("role"):
            by_role[mb["role"]] = entry
        by_name[mb["name"].lower()] = entry
        by_id[mb["id"]] = entry
    by_path: dict[str, dict[str, Any]] = {}
    for entry in by_id.values():
        parts: list[str] = []
        cur: dict[str, Any] | None = entry
        seen: set[str] = set()
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            parts.append(cur["name"])
            cur = by_id.get(cur["parentId"]) if cur["parentId"] else None
        entry["path"] = "/".join(reversed(parts))
        by_path[entry["path"].lower()] = entry
    _FM_MAILBOXES = {
        "by_role": by_role,
        "by_name": by_name,
        "by_id": by_id,
        "by_path": by_path,
    }
    return _FM_MAILBOXES


def _fm_invalidate_mailboxes() -> None:
    global _FM_MAILBOXES
    log.info("jmap: mailbox cache invalidated")
    _FM_MAILBOXES = None


def _fm_lookup_mailbox(name: str) -> dict[str, Any]:
    mbs = _fm_mailboxes()
    key = name.lower()
    if key in mbs["by_role"]:
        return mbs["by_role"][key]
    if key in mbs["by_name"]:
        return mbs["by_name"][key]
    log.warning("mail: mailbox %r not found", name)
    raise ValueError(
        f"mailbox '{name}' not found — try a role like 'inbox' or an exact label name"
    )


def _fmt_addrs(parts: list[dict[str, Any]] | None) -> str:
    return ", ".join(
        f"{p.get('name', '')} <{p.get('email', '')}>"
        if p.get("name")
        else p.get("email", "")
        for p in (parts or [])
    ).strip()


def _fetch_emails(
    filter_obj: dict[str, Any] | None, limit: int
) -> list[dict[str, Any]]:
    session = _fm_session()
    mbs = _fm_mailboxes()
    query_args: dict[str, Any] = {
        "accountId": session["account_id"],
        "sort": [{"property": "receivedAt", "isAscending": False}],
        "limit": limit,
    }
    if filter_obj is not None:
        query_args["filter"] = filter_obj

    log.debug("jmap: Email/query+Email/get filter=%.500s limit=%d", filter_obj, limit)
    t0 = time.monotonic()
    resp = HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                ["Email/query", query_args, "q"],
                [
                    "Email/get",
                    {
                        "accountId": session["account_id"],
                        "#ids": {
                            "resultOf": "q",
                            "name": "Email/query",
                            "path": "/ids",
                        },
                        "properties": [
                            "id",
                            "subject",
                            "from",
                            "to",
                            "receivedAt",
                            "keywords",
                            "preview",
                            "mailboxIds",
                        ],
                    },
                    "g",
                ],
            ],
        },
    )
    resp.raise_for_status()
    responses = {r[2]: r[1] for r in resp.json()["methodResponses"]}
    emails_raw = responses["g"]["list"]
    log.info(
        "jmap: Email/query+Email/get status=%s returned=%d limit=%d latency=%.2fs",
        getattr(resp, "status_code", None),
        len(emails_raw),
        limit,
        time.monotonic() - t0,
    )

    by_id = mbs["by_id"]
    emails = []
    for e in emails_raw:
        folder_ids = list((e.get("mailboxIds") or {}).keys())
        folders = [by_id[fid]["name"] for fid in folder_ids if fid in by_id]
        emails.append(
            {
                "id": e.get("id", ""),
                "subject": e.get("subject", ""),
                "from": _fmt_addrs(e.get("from")),
                "to": _fmt_addrs(e.get("to")),
                "received_at": e.get("receivedAt", ""),
                "unread": "$seen" not in (e.get("keywords") or {}),
                "flagged": "$flagged" in (e.get("keywords") or {}),
                "preview": e.get("preview", ""),
                "folders": folders,
            }
        )
    return emails


class _ListEmailsArgs(BaseModel):
    mailbox: str = Field(
        default="inbox",
        description=(
            "Mailbox to search. Accepts role names (inbox, sent, trash, drafts, archive, junk), "
            "custom label/folder names, or 'all' for every folder (excluding trash/junk/drafts/sent). "
            "Case-insensitive. Default: inbox"
        ),
    )
    unread_only: bool = Field(
        default=False, description="If true, return only unread (unseen) emails"
    )
    from_search: str | None = Field(
        default=None,
        description="Filter by sender name or email address (case-insensitive substring match)",
    )
    limit: int | None = Field(
        default=None,
        description="Maximum number of emails to return (1–50, default 10; default 50 when mailbox='all')",
    )


@tool(
    description=(
        "List and filter emails from Fastmail. Use to answer questions like 'what's in my inbox?', "
        "'how many unread emails do I have?', or 'is there an email from X?'. "
        "Pass mailbox='all' to sweep every folder/label at once (skips trash, junk, drafts, sent) — "
        "use this for cross-folder unread triage. Each result includes 'folders' so the caller can "
        "rank by source (e.g. Newsletters vs Work)."
    ),
    args=_ListEmailsArgs,
)
def list_emails(
    mailbox: str = "inbox",
    unread_only: bool = False,
    from_search: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    log.info(
        "list_emails mailbox=%r unread_only=%s from_search=%r limit=%s",
        mailbox,
        unread_only,
        from_search,
        limit,
    )
    mbs = _fm_mailboxes()
    is_all = mailbox.lower() == "all"

    if limit is None:
        limit = 50 if is_all else 10
    limit = min(max(1, limit), 50)

    conditions: list[dict[str, Any]] = []
    if is_all:
        skip_roles = ("trash", "junk", "drafts", "sent")
        skip_ids = [mbs["by_role"][r]["id"] for r in skip_roles if r in mbs["by_role"]]
        if skip_ids:
            conditions.append({"inMailboxOtherThan": skip_ids})
        mb_meta: dict[str, Any] = {"name": "all", "total": None, "unread": None}
    else:
        mb = _fm_lookup_mailbox(mailbox)
        conditions.append({"inMailbox": mb["id"]})
        mb_meta = {"name": mb["name"], "total": mb["total"], "unread": mb["unread"]}

    if unread_only:
        conditions.append({"notKeyword": "$seen"})
    if from_search:
        conditions.append({"from": from_search})

    if not conditions:
        filter_obj: dict[str, Any] | None = None
    elif len(conditions) == 1:
        filter_obj = conditions[0]
    else:
        filter_obj = {"operator": "AND", "conditions": conditions}

    emails = _fetch_emails(filter_obj, limit)
    log.info("list_emails done mailbox=%s count=%d", mb_meta["name"], len(emails))

    return {
        "mailbox": mb_meta["name"],
        "total_emails": mb_meta["total"],
        "unread_emails": mb_meta["unread"],
        "emails": emails,
    }


_BODY_CHAR_LIMIT = 20000


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "head", "title", "meta", "link"]):
        node.decompose()
    md = markdownify(str(soup), heading_style="ATX")
    lines = [ln.rstrip() for ln in md.splitlines()]
    out: list[str] = []
    blank = 0
    for ln in lines:
        if ln.strip():
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


class _ReadEmailArgs(BaseModel):
    email_id: str = Field(
        description="JMAP email id, as returned in the 'id' field of list_emails results."
    )


@tool(
    description=(
        "Read the full body of a single email by id. Use after list_emails when "
        "you need the actual content (not just the preview snippet). Returns the "
        "plain-text body when available, otherwise HTML converted to markdown. Long "
        f"bodies are truncated at {_BODY_CHAR_LIMIT} characters. Also returns "
        "attachment metadata, with image attachments listed separately under 'images' "
        "(name, type, size, blob_id, cid). Image bytes are not fetched."
    ),
    args=_ReadEmailArgs,
)
def read_email(email_id: str) -> dict[str, Any]:
    log.info("read_email id=%s", email_id)
    session = _fm_session()
    t0 = time.monotonic()
    resp = HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                [
                    "Email/get",
                    {
                        "accountId": session["account_id"],
                        "ids": [email_id],
                        "properties": [
                            "id",
                            "subject",
                            "from",
                            "to",
                            "cc",
                            "receivedAt",
                            "textBody",
                            "htmlBody",
                            "bodyValues",
                            "attachments",
                        ],
                        "fetchTextBodyValues": True,
                        "fetchHTMLBodyValues": True,
                    },
                    "g",
                ],
            ],
        },
    )
    resp.raise_for_status()
    items = resp.json()["methodResponses"][0][1]["list"]
    log.info(
        "jmap: Email/get status=%s items=%d latency=%.2fs",
        getattr(resp, "status_code", None),
        len(items),
        time.monotonic() - t0,
    )
    if not items:
        log.warning("read_email: %s not found", email_id)
        raise ValueError(f"email '{email_id}' not found")
    e = items[0]

    body_values = e.get("bodyValues") or {}
    text_parts = e.get("textBody") or []
    html_parts = e.get("htmlBody") or []

    body = ""
    body_format = "none"
    for part in text_parts:
        bv = body_values.get(part.get("partId"))
        if bv and bv.get("value"):
            body = bv["value"]
            body_format = "text"
            break
    if not body:
        for part in html_parts:
            bv = body_values.get(part.get("partId"))
            if bv and bv.get("value"):
                body = _html_to_text(bv["value"])
                body_format = "html-stripped"
                break

    truncated = False
    if len(body) > _BODY_CHAR_LIMIT:
        body = body[:_BODY_CHAR_LIMIT]
        truncated = True

    log.info(
        "read_email done id=%s subject=%.100r format=%s body=%d chars truncated=%s",
        email_id,
        e.get("subject", ""),
        body_format,
        len(body),
        truncated,
    )
    attachments = []
    images = []
    for a in e.get("attachments") or []:
        mime = a.get("type", "") or ""
        is_image = mime.startswith("image/")
        entry = {
            "name": a.get("name", ""),
            "type": mime,
            "size": a.get("size", 0),
            "blob_id": a.get("blobId", ""),
            "cid": a.get("cid"),
            "disposition": a.get("disposition"),
            "is_image": is_image,
        }
        attachments.append(entry)
        if is_image:
            images.append(entry)

    return {
        "id": e.get("id", ""),
        "subject": e.get("subject", ""),
        "from": _fmt_addrs(e.get("from")),
        "to": _fmt_addrs(e.get("to")),
        "cc": _fmt_addrs(e.get("cc")),
        "received_at": e.get("receivedAt", ""),
        "body": body,
        "body_format": body_format,
        "truncated": truncated,
        "attachments": attachments,
        "images": images,
    }


_last_candidates: set[str] = set()


def _ledger() -> sqlite3.Connection:
    db = sqlite3.connect(xdg_data_dir() / "mail_reported.sqlite3")
    db.execute(
        "CREATE TABLE IF NOT EXISTS reported_messages("
        "message_id TEXT PRIMARY KEY, first_reported_at TEXT NOT NULL)"
    )
    return db


@tool(
    description=(
        "Check Fastmail for emails needing attention: returns only unread-anywhere or flagged-Inbox "
        "emails (excluding trash, junk, drafts, sent) that were not previously reported. After "
        "building the report, call mark_emails_reported with the ids of emails actually included."
    ),
)
def check_new_mail() -> dict[str, Any]:
    global _last_candidates
    log.info("check_new_mail start")
    mbs = _fm_mailboxes()
    skip_ids = [
        mbs["by_role"][r]["id"]
        for r in ("trash", "junk", "drafts", "sent")
        if r in mbs["by_role"]
    ]
    wanted: list[dict[str, Any]] = [{"notKeyword": "$seen"}]
    if "inbox" in mbs["by_role"]:
        wanted.append(
            {
                "operator": "AND",
                "conditions": [
                    {"inMailbox": mbs["by_role"]["inbox"]["id"]},
                    {"hasKeyword": "$flagged"},
                ],
            }
        )
    filter_obj: dict[str, Any] = {"operator": "OR", "conditions": wanted}
    if skip_ids:
        filter_obj = {
            "operator": "AND",
            "conditions": [{"inMailboxOtherThan": skip_ids}, filter_obj],
        }
    emails = _fetch_emails(filter_obj, 50)
    with _ledger() as db:
        seen = {r[0] for r in db.execute("SELECT message_id FROM reported_messages")}
    fetched = len(emails)
    emails = [e for e in emails if e["id"] not in seen]
    _last_candidates = {e["id"] for e in emails}
    log.info(
        "check_new_mail fetched=%d excluded_by_ledger=%d returned=%d (ledger size=%d)",
        fetched,
        fetched - len(emails),
        len(emails),
        len(seen),
    )
    for e in emails:
        log.debug(
            "check_new_mail candidate id=%s from=%s subject=%.100s",
            e["id"],
            e["from"],
            e["subject"],
        )
        if _is_wilma_notification(e) and get_config().wilma_url:
            # `hint` is the generic per-email field telling the model which tool fetches the real content; other integrations should reuse it.
            e["hint"] = (
                "Wilma notification: call find_wilma_message(subject, sent_at) for the full message."
            )
    return {"count": len(emails), "emails": emails}


def _is_wilma_notification(email: dict[str, Any]) -> bool:
    return "inschool.fi" in (
        email.get("from") or ""
    ).lower() and is_notification_subject(email.get("subject") or "")


class _MarkReportedArgs(BaseModel):
    ids: list[str] = Field(
        description="Email ids (from check_new_mail) that were included in the report."
    )


@tool(
    description=(
        "Record emails as reported so check_new_mail will not return them again. Only ids returned "
        "by the most recent check_new_mail are accepted; others come back under 'rejected'. "
        "Never modifies the mailbox."
    ),
    args=_MarkReportedArgs,
)
def mark_emails_reported(ids: list[str]) -> dict[str, Any]:
    clean = list(dict.fromkeys(i for i in ids if isinstance(i, str) and i.strip()))
    recorded = [i for i in clean if i in _last_candidates]
    rejected = [i for i in clean if i not in _last_candidates]
    now = datetime.now(UTC).isoformat()
    log.info(
        "mark_emails_reported requested=%d recorded=%d rejected=%d",
        len(clean),
        len(recorded),
        len(rejected),
    )
    if rejected:
        log.warning(
            "mark_emails_reported rejected ids (not in last check_new_mail): %s",
            rejected,
        )
    with _ledger() as db:
        db.executemany(
            "INSERT OR IGNORE INTO reported_messages(message_id, first_reported_at) VALUES (?, ?)",
            [(i, now) for i in recorded],
        )
    return {"recorded": recorded, "rejected": rejected}


# --- Labels (Fastmail labels are mailboxes; writes confined to the "bot/" subtree) ---

_BOT_ROOT = "bot"


def _method_result(resp: Any) -> dict[str, Any]:
    name, result, _ = resp.json()["methodResponses"][0]
    if name == "error":
        raise ValueError(
            f"Fastmail rejected the request: {result.get('type')} {result.get('description', '')}".strip()
        )
    return result


def _validate_bot_label(label: str) -> list[str]:
    label = label.strip()
    if len(label) > 200:
        raise ValueError("label too long (max 200 chars)")
    if not label.startswith(_BOT_ROOT + "/"):
        raise ValueError('label must start with "bot/", e.g. "bot/job-offer"')
    segs = label.split("/")
    if len(segs) < 2:
        raise ValueError('label must name a child under "bot/"')
    for seg in segs:
        if not seg or seg in (".", "..") or seg != seg.strip():
            raise ValueError(f"invalid label segment {seg!r}")
    return segs


def _mailbox_set(session: dict[str, str], create: dict[str, Any]) -> str:
    log.info("jmap: Mailbox/set create %s", create)
    t0 = time.monotonic()
    resp = HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                [
                    "Mailbox/set",
                    {"accountId": session["account_id"], "create": {"c": create}},
                    "s",
                ]
            ],
        },
    )
    resp.raise_for_status()
    r = _method_result(resp)
    log.info(
        "jmap: Mailbox/set status=%s latency=%.2fs",
        getattr(resp, "status_code", None),
        time.monotonic() - t0,
    )
    if "c" in (r.get("notCreated") or {}):
        log.warning("jmap: Mailbox/set notCreated %s", r["notCreated"]["c"])
        raise ValueError(f"could not create label: {r['notCreated']['c']}")
    log.info(
        "mail: label mailbox created id=%s name=%s",
        r["created"]["c"]["id"],
        create.get("name"),
    )
    return r["created"]["c"]["id"]


def _resolve_bot_label(segments: list[str], create: bool) -> str:
    mbs = _fm_mailboxes()
    parent: str | None = None
    for i, seg in enumerate(segments):
        match = next(
            (
                m
                for m in mbs["by_id"].values()
                if m["parentId"] == parent
                and m["name"].lower() == seg.lower()
                and not (i == 0 and m["role"])
            ),
            None,
        )
        if match is not None:
            parent = match["id"]
            continue
        if not create:
            log.warning("mail: label not found %s", "/".join(segments))
            raise ValueError(f"label not found: {'/'.join(segments)}")
        parent = _mailbox_set(_fm_session(), {"name": seg, "parentId": parent})
        _fm_invalidate_mailboxes()
        mbs = _fm_mailboxes()
    assert parent is not None
    return parent


@tool(
    description=(
        "List all Fastmail labels (mailboxes) with full path, role, total and unread counts. "
        "Read-only. Labels you may add/remove live under 'bot/'."
    ),
)
def list_email_labels() -> list[dict[str, Any]]:
    mbs = _fm_mailboxes()
    log.info("list_email_labels count=%d", len(mbs["by_id"]))
    return [
        {
            "path": m["path"],
            "role": m["role"],
            "total": m["total"],
            "unread": m["unread"],
        }
        for m in sorted(mbs["by_id"].values(), key=lambda m: m["path"].lower())
    ]


class _EmailLabelArgs(BaseModel):
    email_ids: list[str] = Field(description="1-50 JMAP email ids (from list_emails).")
    label: str = Field(
        description='Label path under "bot/", e.g. "bot/job-offer" or "bot/order-tracking".'
    )


def _patch_label(email_ids: list[str], label: str, add: bool) -> dict[str, Any]:
    ids = list(
        dict.fromkeys(i.strip() for i in email_ids if isinstance(i, str) and i.strip())
    )
    op = "add" if add else "remove"
    log.info("%s_email_label label=%r ids=%d", op, label, len(ids))
    if not 1 <= len(ids) <= 50:
        log.warning("%s_email_label rejected: %d ids (need 1-50)", op, len(ids))
        raise ValueError("provide 1-50 email ids")
    try:
        segs = _validate_bot_label(label)
    except ValueError as e:
        log.warning("%s_email_label label validation failed for %r: %s", op, label, e)
        raise
    mbid = _resolve_bot_label(segs, create=add)
    session = _fm_session()
    t0 = time.monotonic()
    resp = HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                [
                    "Email/set",
                    {
                        "accountId": session["account_id"],
                        "update": {
                            i: {f"mailboxIds/{mbid}": True if add else None}
                            for i in ids
                        },
                    },
                    "s",
                ]
            ],
        },
    )
    resp.raise_for_status()
    r = _method_result(resp)
    failed = {
        i: str(e.get("description") or e.get("type") or e)
        for i, e in (r.get("notUpdated") or {}).items()
    }
    log.info(
        "jmap: Email/set %s label=%s status=%s updated=%d failed=%d latency=%.2fs",
        op,
        "/".join(segs),
        getattr(resp, "status_code", None),
        len(ids) - len(failed),
        len(failed),
        time.monotonic() - t0,
    )
    if failed:
        log.warning("%s_email_label failures: %s", op, failed)
    return {
        "label": "/".join(segs),
        "updated": [i for i in ids if i not in failed],
        "failed": failed,
    }


@tool(
    description=(
        'Add a label to emails. Label must be under "bot/" (e.g. "bot/job-offer", "bot/order-tracking"); '
        "missing labels are created. Labelling does not move or delete mail. "
        "Needs a Fastmail token with mail write access."
    ),
    args=_EmailLabelArgs,
)
def add_email_label(email_ids: list[str], label: str) -> dict[str, Any]:
    return _patch_label(email_ids, label, add=True)


@tool(
    description=(
        'Remove a label from emails. Label must be under "bot/" (e.g. "bot/job-offer"). '
        "Does not move or delete mail. Needs a Fastmail token with mail write access."
    ),
    args=_EmailLabelArgs,
)
def remove_email_label(email_ids: list[str], label: str) -> dict[str, Any]:
    return _patch_label(email_ids, label, add=False)
