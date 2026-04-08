import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from registry import tool


@tool(
    description="Roll a die with the specified number of sides (e.g. 6 for a standard die, 20 for a d20).",
    parameters={
        "type": "object",
        "properties": {
            "sides": {
                "type": "integer",
                "description": "Number of sides on the die",
            },
        },
        "required": ["sides"],
    },
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


def _day_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarise a list of timeseries entries for one day."""
    temps = [e["data"]["instant"]["details"]["air_temperature"] for e in entries]
    lo, hi = min(temps), max(temps)

    # Pick the symbol from the noon entry (or closest available) with next_6_hours
    symbol = "unknown"
    for target_hour in (12, 6, 18, 0):
        for e in entries:
            t = e["time"]  # e.g. "2026-04-08T12:00:00Z"
            if int(t[11:13]) == target_hour:
                data = e["data"]
                for period in ("next_6_hours", "next_1_hours"):
                    if period in data and "summary" in data[period]:
                        symbol = data[period]["summary"]["symbol_code"]
                        break
                if symbol != "unknown":
                    break
        if symbol != "unknown":
            break

    return {
        "condition": _normalise_condition(symbol),
        "symbol_code": symbol,
        "temperature_low_c": lo,
        "temperature_high_c": hi,
    }


@tool(
    description="Get the weather for a location: current conditions, today's forecast, and tomorrow's forecast. Accepts a city name or place.",
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "City or place name (e.g. 'Tokyo', 'Oslo', 'New York')",
            },
        },
        "required": ["location"],
    },
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

    # Current conditions
    now = timeseries[0]["data"]
    instant = now["instant"]["details"]
    temp = instant["air_temperature"]
    wind = instant["wind_speed"]
    symbol = "unknown"
    for period in ("next_1_hours", "next_6_hours"):
        if period in now and "summary" in now[period]:
            symbol = now[period]["summary"]["symbol_code"]
            break

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


@tool(
    description="Get the next departing trains from a Finnish railway station by name. Can optionally filter by commuter line letter such as R, I, K, or Z.",
    parameters={
        "type": "object",
        "properties": {
            "station": {
                "type": "string",
                "description": "Station name or short code (e.g. 'Helsinki', 'Tampere', 'HKI')",
            },
            "line": {
                "type": "string",
                "description": "Optional commuter line letter to filter by (e.g. 'R', 'I', 'K', 'Z')",
            },
            "count": {
                "type": "integer",
                "description": "Number of departures to return (default 5, max 20)",
            },
        },
        "required": ["station"],
    },
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
    token = os.environ.get("FASTMAIL_API_TOKEN")
    if not token:
        raise ValueError("FASTMAIL_API_TOKEN environment variable not set")
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
    for mb in mailboxes:
        entry = {
            "id": mb["id"],
            "name": mb["name"],
            "total": mb.get("totalEmails", 0),
            "unread": mb.get("unreadEmails", 0),
        }
        if mb.get("role"):
            by_role[mb["role"]] = entry
        by_name[mb["name"].lower()] = entry
    _FM_MAILBOXES = {"by_role": by_role, "by_name": by_name}
    return _FM_MAILBOXES


def _fm_lookup_mailbox(name: str) -> dict[str, Any]:
    mbs = _fm_mailboxes()
    key = name.lower()
    if key in mbs["by_role"]:
        return mbs["by_role"][key]
    if key in mbs["by_name"]:
        return mbs["by_name"][key]
    raise ValueError(f"mailbox '{name}' not found — try a role like 'inbox' or an exact label name")


@tool(
    description="List and filter emails from a Fastmail mailbox. Use to answer questions like 'what's in my inbox?', 'how many unread emails do I have?', or 'is there an email from X?'.",
    parameters={
        "type": "object",
        "properties": {
            "mailbox": {
                "type": "string",
                "description": "Mailbox to search. Accepts role names (inbox, sent, trash, drafts, archive, junk) or custom label/folder names. Case-insensitive. Default: inbox",
            },
            "unread_only": {
                "type": "boolean",
                "description": "If true, return only unread (unseen) emails",
            },
            "from_search": {
                "type": "string",
                "description": "Filter by sender name or email address (case-insensitive substring match)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of emails to return (1–50, default 10)",
            },
        },
        "required": [],
    },
)
def list_emails(
    mailbox: str = "inbox",
    unread_only: bool = False,
    from_search: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    limit = min(max(1, limit), 50)
    session = _fm_session()
    mb = _fm_lookup_mailbox(mailbox)

    # Build filter
    conditions: list[dict[str, Any]] = [{"inMailbox": mb["id"]}]
    if unread_only:
        conditions.append({"notKeyword": "$seen"})
    if from_search:
        conditions.append({"from": from_search})
    filter_obj: dict[str, Any] = (
        conditions[0] if len(conditions) == 1 else {"operator": "AND", "conditions": conditions}
    )

    resp = _HTTP.post(
        session["api_url"],
        headers={"Authorization": f"Bearer {session['token']}"},
        json={
            "using": ["urn:ietf:params:jmap:core", "urn:ietf:params:jmap:mail"],
            "methodCalls": [
                [
                    "Email/query",
                    {
                        "accountId": session["account_id"],
                        "filter": filter_obj,
                        "sort": [{"property": "receivedAt", "isAscending": False}],
                        "limit": limit,
                    },
                    "q",
                ],
                [
                    "Email/get",
                    {
                        "accountId": session["account_id"],
                        "#ids": {"resultOf": "q", "name": "Email/query", "path": "/ids"},
                        "properties": ["id", "subject", "from", "receivedAt", "keywords", "preview"],
                    },
                    "g",
                ],
            ],
        },
    )
    resp.raise_for_status()
    responses = {r[2]: r[1] for r in resp.json()["methodResponses"]}
    emails_raw = responses["g"]["list"]

    emails = []
    for e in emails_raw:
        frm = e.get("from") or []
        from_str = ", ".join(
            f"{p.get('name', '')} <{p.get('email', '')}>" if p.get("name") else p.get("email", "")
            for p in frm
        ).strip()
        emails.append(
            {
                "subject": e.get("subject", ""),
                "from": from_str,
                "received_at": e.get("receivedAt", ""),
                "unread": "$seen" not in (e.get("keywords") or {}),
                "preview": e.get("preview", ""),
            }
        )

    return {
        "mailbox": mb["name"],
        "total_emails": mb["total"],
        "unread_emails": mb["unread"],
        "emails": emails,
    }
