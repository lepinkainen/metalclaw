import random
from datetime import datetime, timedelta, timezone

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


def _geocode(location: str) -> tuple[float, float, str]:
    """Resolve a place name to (lat, lon, display_name) via Nominatim."""
    resp = _HTTP.get(_NOMINATIM, params={"q": location, "format": "json", "limit": 1})
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not find location: {location}")
    hit = results[0]
    return round(float(hit["lat"]), 4), round(float(hit["lon"]), 4), hit["display_name"]


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
def _day_summary(entries: list) -> str:
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

    condition = symbol.replace("_", " ").removesuffix(" day").removesuffix(" night")
    return f"  {condition}, {lo}–{hi}°C"


def weather(location: str) -> str:
    lat, lon, display_name = _geocode(location)

    resp = _HTTP.get(_METNO, params={"lat": lat, "lon": lon})
    resp.raise_for_status()
    timeseries = resp.json()["properties"]["timeseries"]

    today = datetime.now(timezone.utc).date()
    tomorrow = today + timedelta(days=1)

    by_day: dict[str, list] = {}
    for entry in timeseries:
        d = entry["time"][:10]
        by_day.setdefault(d, []).append(entry)

    today_str = today.isoformat()
    tomorrow_str = tomorrow.isoformat()

    lines = [f"Weather for {display_name}:"]

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
    condition = symbol.replace("_", " ").removesuffix(" day").removesuffix(" night")
    lines.append(f"Now: {condition}, {temp}°C, wind {wind} m/s")

    if today_str in by_day:
        lines.append(f"Today:{_day_summary(by_day[today_str])}")
    if tomorrow_str in by_day:
        lines.append(f"Tomorrow:{_day_summary(by_day[tomorrow_str])}")

    return "\n".join(lines)


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
    description="Get the next departing trains from a Finnish railway station by name.",
    parameters={
        "type": "object",
        "properties": {
            "station": {
                "type": "string",
                "description": "Station name or short code (e.g. 'Helsinki', 'Tampere', 'HKI')",
            },
            "count": {
                "type": "integer",
                "description": "Number of departures to return (default 5, max 20)",
            },
        },
        "required": ["station"],
    },
)
def train_departures(station: str, count: int = 5) -> str:
    count = min(max(1, count), 20)
    code, full_name = _find_station_code(station)

    resp = _HTTP.get(
        f"{_DIGITRAFFIC}/live-trains/station/{code}",
        params={
            "departing_trains": count,
            "departed_trains": 0,
            "arriving_trains": 0,
            "arrived_trains": 0,
        },
    )
    resp.raise_for_status()
    trains = resp.json()

    if not trains:
        return f"No upcoming departures found for {full_name} ({code})."

    lines = [f"Departures from {full_name} ({code}):"]
    for train in trains:
        train_type = train.get("trainType", "")
        train_number = train.get("trainNumber", "")
        name = f"{train_type} {train_number}".strip()

        # Find the departure row for this station
        dep_row = next(
            (
                r for r in train.get("timeTableRows", [])
                if r["stationShortCode"] == code and r["type"] == "DEPARTURE"
            ),
            None,
        )
        if dep_row is None:
            continue

        scheduled = dep_row["scheduledTime"][11:16]  # HH:MM from ISO string
        estimate = dep_row.get("liveEstimateTime")
        time_str = scheduled
        if estimate and estimate != dep_row["scheduledTime"]:
            time_str += f" (est. {estimate[11:16]})"

        track = dep_row.get("commercialTrack", "?")

        # Find destination: last ARRIVAL row in the timetable
        arrival_rows = [r for r in train["timeTableRows"] if r["type"] == "ARRIVAL"]
        destination = arrival_rows[-1]["stationShortCode"] if arrival_rows else "?"

        cancelled = " [CANCELLED]" if train.get("cancelled") else ""
        lines.append(f"  {time_str}  {name:<10}  track {track}  -> {destination}{cancelled}")

    return "\n".join(lines)
