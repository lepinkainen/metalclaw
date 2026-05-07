"""Model-facing heartbeat-action management tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

import chat_loop
import heartbeat
from config import get_config
from registry import tool


class _CronScheduleArgs(BaseModel):
    days: list[str] = Field(
        description=(
            "Weekdays the action fires on, three-letter abbreviations: "
            "'mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'."
        )
    )
    time: str = Field(
        description="Local fire time as 'HH:MM' (24h)."
    )
    timezone: str = Field(
        description="IANA timezone, e.g. 'Europe/Helsinki', 'UTC'."
    )


class _CreateHeartbeatActionArgs(BaseModel):
    kind: Literal["at", "cron", "every"] = Field(
        description=(
            "Action kind. 'at' = one-shot at a single timestamp; 'cron' = "
            "calendar-recurring on selected weekdays at one local time; "
            "'every' = interval-recurring on a fixed cadence."
        )
    )
    prompt: str = Field(
        description=(
            "Prompt run on each fire. The model receives this in heartbeat "
            "mode and is expected to either reply HEARTBEAT_OK (no alert) "
            "or with a concise alert the user should see."
        )
    )
    at: str | None = Field(
        default=None,
        description=(
            "Required when kind='at'. ISO-8601 timestamp (e.g. "
            "'2026-05-08T15:30:00+03:00'). Tz-naive values are treated as UTC."
        ),
    )
    schedule: _CronScheduleArgs | None = Field(
        default=None,
        description="Required when kind='cron'.",
    )
    every: int | None = Field(
        default=None,
        description="Required when kind='every'. Cadence in seconds (>0).",
    )
    channel: str | None = Field(
        default=None,
        description=(
            "Optional channel address for notifications, e.g. 'cli', "
            "'telegram-123456', 'discord-987654'. Defaults to the active "
            "frontend scope, then config.heartbeat_default_channel."
        ),
    )


def _resolve_channel(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    scope = chat_loop.current_scope.get()
    if scope:
        return scope
    cfg = get_config()
    if cfg.heartbeat_default_channel:
        return cfg.heartbeat_default_channel
    raise ValueError(
        "no channel resolvable — pass channel=... or set "
        "heartbeat_default_channel in config.yaml"
    )


@tool(
    description=(
        "Schedule a proactive heartbeat action. Use 'at' for one-shot "
        "reminders at a specific timestamp, 'cron' for calendar-based "
        "recurring checks (e.g. weekday mornings), 'every' for interval-"
        "based watches (e.g. every 30 minutes). The action is stored in "
        "the bot-owned ledger and runs on the heartbeat scheduler."
    ),
    args=_CreateHeartbeatActionArgs,
)
def create_heartbeat_action(
    kind: str,
    prompt: str,
    at: str | None = None,
    schedule: dict[str, Any] | None = None,
    every: int | None = None,
    channel: str | None = None,
) -> dict[str, Any]:
    try:
        action_kind = heartbeat.ActionKind(kind)
    except ValueError as e:
        return {"error": "invalid_kind", "message": str(e)}

    cron: heartbeat.CronSchedule | None = None
    if action_kind == heartbeat.ActionKind.CRON:
        if not schedule:
            return {"error": "missing_schedule", "message": "kind=cron requires schedule"}
        try:
            cron = heartbeat.CronSchedule(
                days=heartbeat.normalise_weekdays(schedule.get("days") or []),
                time=heartbeat.validate_time_string(str(schedule.get("time", ""))),
                timezone=heartbeat.validate_timezone(str(schedule.get("timezone", ""))),
            )
        except ValueError as e:
            return {"error": "invalid_schedule", "message": str(e)}

    try:
        resolved_channel = _resolve_channel(channel)
    except ValueError as e:
        return {"error": "no_channel", "message": str(e)}

    scope = chat_loop.current_scope.get()
    try:
        action = heartbeat.create_action(
            kind=action_kind,
            prompt=prompt,
            channel=resolved_channel,
            created_from=scope,
            at=at,
            schedule=cron,
            every=every,
        )
    except ValueError as e:
        return {"error": "invalid_arguments", "message": str(e)}
    return {"status": "created", "action": action.to_dict()}


class _ListHeartbeatActionsArgs(BaseModel):
    include_completed: bool = Field(
        default=False,
        description="Include actions that have already completed (one-shots that fired).",
    )


@tool(
    description=(
        "List the user's currently scheduled heartbeat actions. Pass "
        "include_completed=true to also see archived one-shots."
    ),
    args=_ListHeartbeatActionsArgs,
)
def list_heartbeat_actions(include_completed: bool = False) -> dict[str, Any]:
    ledger = heartbeat.load_ledger()
    out: dict[str, Any] = {"active": [a.to_dict() for a in ledger.actions]}
    if include_completed:
        out["completed"] = list(ledger.completed)
    return out


class _CancelHeartbeatActionArgs(BaseModel):
    action_id: str = Field(description="Six-character action id (from list_heartbeat_actions).")


@tool(
    description=(
        "Cancel a scheduled heartbeat action by id. Returns status='cancelled' "
        "with the removed action, or status='not_found' if the id does not match."
    ),
    args=_CancelHeartbeatActionArgs,
)
def cancel_heartbeat_action(action_id: str) -> dict[str, Any]:
    removed = heartbeat.cancel(action_id.strip())
    if removed is None:
        return {"status": "not_found", "action_id": action_id}
    return {"status": "cancelled", "action": removed.to_dict()}
