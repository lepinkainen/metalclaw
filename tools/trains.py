from typing import Any

from pydantic import BaseModel, Field

from registry import tool

from ._http import HTTP

_DIGITRAFFIC = "https://rata.digitraffic.fi/api/v1"


def _find_station_code(name: str) -> tuple[str, str]:
    """Return (shortCode, stationName) for the first station matching `name`."""
    resp = HTTP.get(f"{_DIGITRAFFIC}/metadata/stations")
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

    resp = HTTP.get(
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
