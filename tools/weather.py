from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from registry import tool

from ._http import HTTP

_NOMINATIM = "https://nominatim.openstreetmap.org/search"
_METNO = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


def _geocode(location: str) -> tuple[float, float, str]:
    """Resolve a place name to (lat, lon, display_name) via Nominatim."""
    resp = HTTP.get(_NOMINATIM, params={"q": location, "format": "json", "limit": 1})
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

    resp = HTTP.get(_METNO, params={"lat": lat, "lon": lon})
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
