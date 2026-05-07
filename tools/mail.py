from typing import Any

from pydantic import BaseModel, Field

from config import get_config
from registry import tool

from ._http import HTTP

_FASTMAIL_SESSION_URL = "https://api.fastmail.com/jmap/session"

_FM_SESSION: dict[str, str] | None = None
_FM_MAILBOXES: dict[str, Any] | None = None


def _fm_session() -> dict[str, str]:
    global _FM_SESSION
    if _FM_SESSION is not None:
        return _FM_SESSION
    token = get_config().fastmail_api_token
    if not token:
        raise ValueError(
            "Fastmail API token not configured. Set fastmail_api_token in config.yaml "
            "or FASTMAIL_API_TOKEN env var."
        )
    resp = HTTP.get(_FASTMAIL_SESSION_URL, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    data = resp.json()
    _FM_SESSION = {
        "api_url": data["apiUrl"],
        "account_id": data["primaryAccounts"]["urn:ietf:params:jmap:mail"],
        "token": token,
    }
    return _FM_SESSION


def _fm_mailboxes() -> dict[str, Any]:
    global _FM_MAILBOXES
    if _FM_MAILBOXES is not None:
        return _FM_MAILBOXES
    session = _fm_session()
    resp = HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                ["Mailbox/get", {"accountId": session["account_id"], "ids": None}, "mb"],
            ],
        },
    )
    resp.raise_for_status()
    mailboxes = resp.json()["methodResponses"][0][1]["list"]
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
        }
        if mb.get("role"):
            by_role[mb["role"]] = entry
        by_name[mb["name"].lower()] = entry
        by_id[mb["id"]] = entry
    _FM_MAILBOXES = {"by_role": by_role, "by_name": by_name, "by_id": by_id}
    return _FM_MAILBOXES


def _fm_lookup_mailbox(name: str) -> dict[str, Any]:
    mbs = _fm_mailboxes()
    key = name.lower()
    if key in mbs["by_role"]:
        return mbs["by_role"][key]
    if key in mbs["by_name"]:
        return mbs["by_name"][key]
    raise ValueError(f"mailbox '{name}' not found — try a role like 'inbox' or an exact label name")


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
    session = _fm_session()
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

    query_args: dict[str, Any] = {
        "accountId": session["account_id"],
        "sort": [{"property": "receivedAt", "isAscending": False}],
        "limit": limit,
    }
    if filter_obj is not None:
        query_args["filter"] = filter_obj

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
                        "#ids": {"resultOf": "q", "name": "Email/query", "path": "/ids"},
                        "properties": [
                            "id",
                            "subject",
                            "from",
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

    by_id = mbs["by_id"]
    emails = []
    for e in emails_raw:
        frm = e.get("from") or []
        from_str = ", ".join(
            f"{p.get('name', '')} <{p.get('email', '')}>" if p.get("name") else p.get("email", "")
            for p in frm
        ).strip()
        folder_ids = list((e.get("mailboxIds") or {}).keys())
        folders = [by_id[fid]["name"] for fid in folder_ids if fid in by_id]
        emails.append(
            {
                "id": e.get("id", ""),
                "subject": e.get("subject", ""),
                "from": from_str,
                "received_at": e.get("receivedAt", ""),
                "unread": "$seen" not in (e.get("keywords") or {}),
                "preview": e.get("preview", ""),
                "folders": folders,
            }
        )

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
    session = _fm_session()
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
    if not items:
        raise ValueError(f"email '{email_id}' not found")
    e = items[0]

    def _addrs(parts: list[dict[str, Any]] | None) -> str:
        return ", ".join(
            f"{p.get('name', '')} <{p.get('email', '')}>" if p.get("name") else p.get("email", "")
            for p in (parts or [])
        ).strip()

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
        "from": _addrs(e.get("from")),
        "to": _addrs(e.get("to")),
        "cc": _addrs(e.get("cc")),
        "received_at": e.get("receivedAt", ""),
        "body": body,
        "body_format": body_format,
        "truncated": truncated,
        "attachments": attachments,
        "images": images,
    }
