import random

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
    description="Get the current weather for a location. Accepts a city name or place.",
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
def weather(location: str) -> str:
    lat, lon, display_name = _geocode(location)

    resp = _HTTP.get(_METNO, params={"lat": lat, "lon": lon})
    resp.raise_for_status()
    data = resp.json()

    now = data["properties"]["timeseries"][0]["data"]
    instant = now["instant"]["details"]

    temp = instant["air_temperature"]
    humidity = instant["relative_humidity"]
    wind = instant["wind_speed"]
    wind_dir = instant.get("wind_from_direction", "?")

    symbol = "unknown"
    for period in ("next_1_hours", "next_6_hours"):
        if period in now and "summary" in now[period]:
            symbol = now[period]["summary"]["symbol_code"]
            break

    condition = symbol.replace("_", " ").removesuffix(" day").removesuffix(" night")

    return (
        f"Weather for {display_name}:\n"
        f"  Condition: {condition}\n"
        f"  Temperature: {temp}°C\n"
        f"  Humidity: {humidity}%\n"
        f"  Wind: {wind} m/s (from {wind_dir}°)"
    )
