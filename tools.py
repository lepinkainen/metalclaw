import random
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from pydantic import BaseModel, Field

import memory
import vault_search
from config import get_config
from registry import tool


class _RollDieArgs(BaseModel):
    sides: int = Field(description="Number of sides on the die")


@tool(
    description="Roll a die with the specified number of sides (e.g. 6 for a standard die, 20 for a d20).",
    args=_RollDieArgs,
)
def roll_die(sides: int) -> str:
    result = random.randint(1, sides)
    return f"Rolled a d{sides}: {result}"


_HTTP = httpx.Client(
    headers={"User-Agent": "metalclaw/0.1 github.com/shrike/metalclaw"},
    timeout=15.0,
)

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_METNO = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
_DIGITRAFFIC = "https://rata.digitraffic.fi/api/v1"
_FASTMAIL_SESSION_URL = "https://api.fastmail.com/jmap/session"

_FM_SESSION: dict[str, str] | None = None
_FM_MAILBOXES: dict[str, Any] | None = None


def _geocode(location: str) -> tuple[float, float, str]:
    """Resolve a place name to (lat, lon, display_name) via Nominatim."""
    resp = _HTTP.get(_NOMINATIM, params={"q": location, "format": "json", "limit": 1})
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not find location: {location}")
    hit = results[0]
    return round(float(hit["lat"]), 4), round(float(hit["lon"]), 4), hit["display_name"]


def _normalise_condition(symbol: str) -> str:
    return symbol.replace("_", " ").removesuffix(" day").removesuffix(" night")


def _extract_symbol(data: dict[str, Any], periods: tuple[str, ...]) -> str | None:
    """Return the first available ``symbol_code`` across ``periods``."""
    for period in periods:
        block = data.get(period)
        if block and "summary" in block:
            return block["summary"]["symbol_code"]
    return None


def _day_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise a list of timeseries entries for one day."""
    temps = [e["data"]["instant"]["details"]["air_temperature"] for e in entries]
    lo, hi = min(temps), max(temps)

    # Prefer 6-hour outlook from noon (or closest fallback hour).
    symbol = "unknown"
    for target_hour in (12, 6, 18, 0):
        for e in entries:
            if int(e["time"][11:13]) == target_hour:
                found = _extract_symbol(e["data"], ("next_6_hours", "next_1_hours"))
                if found:
                    symbol = found
                    break
        if symbol != "unknown":
            break

    return {
        "condition": _normalise_condition(symbol),
        "symbol_code": symbol,
        "temperature_low_c": lo,
        "temperature_high_c": hi,
    }


class _WeatherArgs(BaseModel):
    location: str = Field(description="City or place name (e.g. 'Tokyo', 'Oslo', 'New York')")


@tool(
    description="Get the weather for a location: current conditions, today's forecast, and tomorrow's forecast. Accepts a city name or place.",
    args=_WeatherArgs,
)
def weather(location: str) -> dict[str, Any]:
    lat, lon, display_name = _geocode(location)

    resp = _HTTP.get(_METNO, params={"lat": lat, "lon": lon})
    resp.raise_for_status()
    timeseries = resp.json()["properties"]["timeseries"]

    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)

    by_day: dict[str, list[dict[str, Any]]] = {}
    for entry in timeseries:
        d = entry["time"][:10]
        by_day.setdefault(d, []).append(entry)

    today_str = today.isoformat()
    tomorrow_str = tomorrow.isoformat()

    # Current conditions: prefer 1-hour outlook (more recent) over 6-hour fallback.
    now = timeseries[0]["data"]
    instant = now["instant"]["details"]
    temp = instant["air_temperature"]
    wind = instant["wind_speed"]
    symbol = _extract_symbol(now, ("next_1_hours", "next_6_hours")) or "unknown"

    result: dict[str, Any] = {
        "source": {
            "name": "MET Norway Locationforecast + OpenStreetMap Nominatim",
            "realtime": False,
            "note": "Forecast and current conditions from the weather API, resolved via geocoding.",
        },
        "location": {
            "query": location,
            "display_name": display_name,
            "latitude": lat,
            "longitude": lon,
        },
        "current": {
            "condition": _normalise_condition(symbol),
            "symbol_code": symbol,
            "temperature_c": temp,
            "wind_m_s": wind,
        },
        "today": None,
        "tomorrow": None,
    }

    if today_str in by_day:
        result["today"] = _day_summary(by_day[today_str])
    if tomorrow_str in by_day:
        result["tomorrow"] = _day_summary(by_day[tomorrow_str])

    return result


def _find_station_code(name: str) -> tuple[str, str]:
    """Return (shortCode, stationName) for the first station matching `name`."""
    resp = _HTTP.get(f"{_DIGITRAFFIC}/metadata/stations")
    resp.raise_for_status()
    needle = name.lower()
    for s in resp.json():
        if not s.get("passengerTraffic"):
            continue
        if needle in s["stationName"].lower() or needle == s["stationShortCode"].lower():
            return s["stationShortCode"], s["stationName"]
    raise ValueError(f"No passenger station found matching: {name}")


class _TrainDeparturesArgs(BaseModel):
    station: str = Field(description="Station name or short code (e.g. 'Helsinki', 'Tampere', 'HKI')")
    line: str | None = Field(
        default=None,
        description="Optional commuter line letter to filter by (e.g. 'R', 'I', 'K', 'Z')",
    )
    count: int = Field(default=5, description="Number of departures to return (default 5, max 20)")


@tool(
    description="Get the next departing trains from a Finnish railway station by name. Can optionally filter by commuter line letter such as R, I, K, or Z.",
    args=_TrainDeparturesArgs,
)
def train_departures(station: str, count: int = 5, line: str | None = None) -> dict[str, Any]:
    count = min(max(1, count), 20)
    code, full_name = _find_station_code(station)
    line = line.strip().upper() if line else None

    params = {
        "departing_trains": max(count, 20) if line else count,
        "departed_trains": 0,
        "arriving_trains": 0,
        "arrived_trains": 0,
    }
    if line:
        params["train_categories"] = "Commuter"

    resp = _HTTP.get(
        f"{_DIGITRAFFIC}/live-trains/station/{code}",
        params=params,
    )
    resp.raise_for_status()
    trains = resp.json()

    if line:
        trains = [t for t in trains if (t.get("commuterLineID") or "").upper() == line]

    departures: list[dict[str, Any]] = []
    for train in trains:
        dep_row = next(
            (
                r for r in train.get("timeTableRows", [])
                if r["stationShortCode"] == code
                and r["type"] == "DEPARTURE"
                and r.get("commercialStop", True)
            ),
            None,
        )
        if dep_row is None:
            continue

        arrival_rows = [r for r in train["timeTableRows"] if r["type"] == "ARRIVAL"]
        destination = arrival_rows[-1]["stationShortCode"] if arrival_rows else "?"

        departures.append(
            {
                "line": train.get("commuterLineID"),
                "train_type": train.get("trainType", ""),
                "train_number": train.get("trainNumber", ""),
                "scheduled_time": dep_row["scheduledTime"],
                "estimated_time": dep_row.get("liveEstimateTime"),
                "actual_time": dep_row.get("actualTime"),
                "track": dep_row.get("commercialTrack", "?"),
                "destination_code": destination,
                "cancelled": bool(train.get("cancelled") or dep_row.get("cancelled")),
                "delay_minutes": dep_row.get("differenceInMinutes"),
            }
        )
        if len(departures) >= count:
            break

    return {
        "source": {
            "name": "Digitraffic Rail",
            "realtime": True,
            "note": "Live departure data from the Digitraffic rail API. This should match departure boards that use the same underlying data source.",
        },
        "station": {
            "query": station,
            "code": code,
            "name": full_name,
        },
        "line_filter": line,
        "count": count,
        "departures": departures,
    }


# --- Fastmail JMAP ---


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
    resp = _HTTP.get(_FASTMAIL_SESSION_URL, headers={"Authorization": f"Bearer {token}"})
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
    resp = _HTTP.post(
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

    resp = _HTTP.post(
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
    resp = _HTTP.post(
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


# --- User memory ---


class _SetUserPreferenceArgs(BaseModel):
    key: str = Field(description="Short identifier, e.g. 'role', 'tone', 'interests'")
    value: str = Field(
        description="Value for this preference. May contain Obsidian [[wikilinks]]."
    )


@tool(
    description=(
        "Save a structured user preference (key/value) to long-term memory. "
        "Use for stable facts about how the user wants to be addressed or what "
        "they care about, e.g. role, tone, interests, timezone."
    ),
    args=_SetUserPreferenceArgs,
)
def set_user_preference(key: str, value: str) -> dict[str, Any]:
    memory.set_preference(key, value)
    return {"status": "saved", "key": key, "value": value}


class _AddUserFactArgs(BaseModel):
    text: str = Field(description="The fact to remember. May contain Obsidian [[wikilinks]].")


@tool(
    description=(
        "Append a free-form fact about the user to long-term memory. "
        "Use for one-off facts that don't fit a key/value preference."
    ),
    args=_AddUserFactArgs,
)
def add_user_fact(text: str) -> dict[str, Any]:
    memory.add_fact(text)
    return {"status": "saved", "text": text}


class _AddUserInstructionArgs(BaseModel):
    text: str = Field(
        description=(
            "Durable behavioural rule the assistant must follow on every "
            "future turn, phrased as an imperative (e.g. 'Always reply in "
            "Finnish unless the user writes in English.', 'Use metric units "
            "for distances.'). May contain Obsidian [[wikilinks]]."
        )
    )


@tool(
    description=(
        "Save a durable instruction the assistant must follow on every "
        "subsequent turn. Use this for behavioural rules — how to respond, "
        "what to avoid, formatting preferences — not for facts about the "
        "user (use add_user_fact for those) or key/value preferences (use "
        "set_user_preference). Stored in the ## Instructions section of the "
        "memory file and re-injected into the system prompt."
    ),
    args=_AddUserInstructionArgs,
)
def add_user_instruction(text: str) -> dict[str, Any]:
    memory.add_instruction(text)
    return {"status": "saved", "text": text}


class _ForgetUserMemoryArgs(BaseModel):
    matcher: str = Field(description="Substring to match against memory entries.")


@tool(
    description=(
        "Delete a single entry from the user's long-term memory by "
        "case-insensitive substring match against the key, value, fact text, "
        "or instruction text. Forget is a final operation — if the matcher "
        "hits more than one entry the call returns status='ambiguous' with "
        "the candidate list and deletes nothing; refine the matcher and "
        "retry. Returns status='removed' with the deleted entry on a unique "
        "match, or status='not_found' if nothing matched."
    ),
    args=_ForgetUserMemoryArgs,
)
def forget_user_memory(matcher: str) -> dict[str, Any]:
    res = memory.forget(matcher)
    out: dict[str, Any] = {"status": res.status, "matcher": matcher}
    if res.entry is not None:
        out["entry"] = res.entry
    if res.matches:
        out["matches"] = res.matches
    return out


@tool(
    description=(
        "Read the full long-term memory file for this user. Returns the raw "
        "Obsidian-flavoured markdown so you can reason about preferences, facts, "
        "and instructions stored across sessions."
    ),
)
def get_user_memory() -> dict[str, Any]:
    return {"markdown": memory.render_full()}


# --- Obsidian vault search ---


class _SearchVaultArgs(BaseModel):
    query: str = Field(description="Ripgrep regex or literal text to search for.")
    max_results: int = Field(
        default=20, description="Maximum number of hits to return (default 20, max 200)."
    )
    context_lines: int = Field(
        default=1,
        description="Lines of context before and after each match (default 1, max 10).",
    )


@tool(
    description=(
        "Search the user's Obsidian vault for notes matching a query (ripgrep "
        "regex or literal text). Returns snippets with file paths and line "
        "numbers. Use read_note afterwards to fetch the full body of a "
        "promising hit."
    ),
    args=_SearchVaultArgs,
)
def search_vault(
    query: str,
    max_results: int = 20,
    context_lines: int = 1,
) -> dict[str, Any]:
    return vault_search.search(query, max_results=max_results, context_lines=context_lines)


class _ReadNoteArgs(BaseModel):
    path: str = Field(
        description="Path relative to the vault root, e.g. 'Projects/Metalclaw.md'."
    )


@tool(
    description=(
        "Read a markdown note from the user's Obsidian vault by path relative "
        "to the vault root (e.g. 'Projects/Metalclaw.md'). Refuses paths "
        "outside the vault and non-markdown files."
    ),
    args=_ReadNoteArgs,
)
def read_note(path: str) -> dict[str, Any]:
    return vault_search.read(path)


# --- Escalation ---


class _EscalateArgs(BaseModel):
    query: str = Field(description="The user's question or task, restated.")
    reason: str = Field(description="Why you are escalating instead of answering.")


@tool(
    description=(
        "Escalate to a more capable cloud model. Use ONLY when you genuinely "
        "cannot answer or the task needs reasoning beyond your capability. "
        "Pass the user's question and a brief reason. Do NOT use for trivial "
        "requests."
    ),
    args=_EscalateArgs,
)
def escalate_to_big_model(query: str, reason: str) -> dict[str, Any]:
    cfg = get_config()
    if not cfg.escalation_enabled:
        return {"status": "disabled", "message": "Escalation disabled in config."}

    # Lazy import to avoid circular dependency (tools.py loads before bot.py
    # has finished setup, and bot.py imports tools at runtime).
    import bot
    from providers import get_provider

    snapshot = bot._active_session_messages.get()
    if snapshot is None:
        sub_messages: list[dict] = [{"role": "user", "content": query}]
    else:
        sub_messages = list(snapshot)
        sub_messages.append(
            {"role": "user", "content": f"[escalation: {reason}] {query}"}
        )

    big = get_provider(cfg.escalation_provider, model_override=cfg.escalation_model)
    reply = bot._chat_with_provider(
        big, sub_messages, exclude_tools={"escalate_to_big_model"}
    )
    return {
        "status": "ok",
        "model": f"{cfg.escalation_provider}:{cfg.escalation_model}",
        "reason": reason,
        "reply": reply,
    }
