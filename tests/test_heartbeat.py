import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import yaml

import channels
import chat_loop
import config
import heartbeat


@pytest.fixture
def cfg(tmp_path, monkeypatch, clear_env, write_config):
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        vault_path=str(tmp_path / "vault"),
        memory_subdir="Mem",
        heartbeat_interval_seconds=60,
        heartbeat_default_channel="cli",
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfgdir"))
    config.reset_cache()
    (tmp_path / "vault" / "Mem").mkdir(parents=True)
    yield config.get_config()
    config.reset_cache()
    channels.CHANNELS.clear()


# --- parse_interval ---


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30s", 30),
        ("5m", 300),
        ("2h", 7200),
        ("1d", 86400),
        ("90", 90),
        (120, 120),
    ],
)
def test_parse_interval(raw, expected):
    assert heartbeat.parse_interval(raw) == expected


def test_parse_interval_bad():
    with pytest.raises(ValueError):
        heartbeat.parse_interval("forever")


def test_parse_interval_zero_rejected():
    with pytest.raises(ValueError):
        heartbeat.parse_interval(0)
    with pytest.raises(ValueError):
        heartbeat.parse_interval("0s")


# --- weekday / time / timezone validation ---


def test_normalise_weekdays_dedupes_and_lowercases():
    assert heartbeat.normalise_weekdays(["Mon", "thu", "MON"]) == ("mon", "thu")


def test_normalise_weekdays_rejects_unknown():
    with pytest.raises(ValueError):
        heartbeat.normalise_weekdays(["funday"])


def test_normalise_weekdays_rejects_empty():
    with pytest.raises(ValueError):
        heartbeat.normalise_weekdays([])


def test_validate_time_string():
    assert heartbeat.validate_time_string("07:30") == "07:30"
    with pytest.raises(ValueError):
        heartbeat.validate_time_string("25:00")
    with pytest.raises(ValueError):
        heartbeat.validate_time_string("noon")


def test_validate_timezone():
    assert heartbeat.validate_timezone("Europe/Helsinki") == "Europe/Helsinki"
    with pytest.raises(ValueError):
        heartbeat.validate_timezone("Atlantis/Lost")


# --- ledger I/O ---


def test_ledger_round_trip(cfg):
    assert heartbeat.load_ledger().actions == []
    a = heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=60,
    )
    fresh = heartbeat.load_ledger()
    assert [x.id for x in fresh.actions] == [a.id]
    assert fresh.actions[0].prompt == "poll"
    raw = yaml.safe_load((cfg.memory_dir / "heartbeat.yaml").read_text())
    assert raw["version"] == 1
    assert raw["actions"][0]["kind"] == "every"


def test_create_action_at_requires_at(cfg):
    with pytest.raises(ValueError):
        heartbeat.create_action(
            kind=heartbeat.ActionKind.AT,
            prompt="ping",
            channel="cli",
            created_from="cli",
        )


def test_create_action_cron_requires_schedule(cfg):
    with pytest.raises(ValueError):
        heartbeat.create_action(
            kind=heartbeat.ActionKind.CRON,
            prompt="ping",
            channel="cli",
            created_from="cli",
        )


def test_create_action_every_requires_positive(cfg):
    with pytest.raises(ValueError):
        heartbeat.create_action(
            kind=heartbeat.ActionKind.EVERY,
            prompt="ping",
            channel="cli",
            created_from="cli",
            every=-5,
        )


def test_create_action_blank_prompt_rejected(cfg):
    with pytest.raises(ValueError):
        heartbeat.create_action(
            kind=heartbeat.ActionKind.EVERY,
            prompt="   ",
            channel="cli",
            created_from="cli",
            every=60,
        )


def test_cancel_known_action(cfg):
    a = heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=60,
    )
    removed = heartbeat.cancel(a.id)
    assert removed is not None and removed.id == a.id
    assert heartbeat.load_ledger().actions == []


def test_cancel_unknown_action(cfg):
    assert heartbeat.cancel("ffffff") is None


# --- state file ---


def test_state_round_trip(cfg):
    assert heartbeat.load_state() == {}
    heartbeat.save_state({"abc123": "2026-05-07T12:00:00+00:00"})
    assert heartbeat.load_state() == {"abc123": "2026-05-07T12:00:00+00:00"}


def test_state_legacy_flat_format(cfg):
    path = heartbeat._state_path()
    path.write_text(json.dumps({"abc123": "2026-05-07T12:00:00+00:00"}))
    assert heartbeat.load_state() == {"abc123": "2026-05-07T12:00:00+00:00"}


# --- due logic ---


def _at(action_id="abc123", at_iso=None):
    return heartbeat.HeartbeatAction(
        id=action_id,
        kind=heartbeat.ActionKind.AT,
        prompt="x",
        channel="cli",
        created_at="2026-05-07T00:00:00+00:00",
        created_from="cli",
        at=at_iso,
    )


def _every(every=60, action_id="abc123"):
    return heartbeat.HeartbeatAction(
        id=action_id,
        kind=heartbeat.ActionKind.EVERY,
        prompt="x",
        channel="cli",
        created_at="2026-05-07T00:00:00+00:00",
        created_from="cli",
        every=every,
    )


def _cron(days=("mon",), time="07:00", tz="UTC", action_id="abc123"):
    return heartbeat.HeartbeatAction(
        id=action_id,
        kind=heartbeat.ActionKind.CRON,
        prompt="x",
        channel="cli",
        created_at="2026-05-07T00:00:00+00:00",
        created_from="cli",
        schedule=heartbeat.CronSchedule(days=days, time=time, timezone=tz),
    )


def test_at_due_after_target():
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    assert heartbeat.is_action_due(_at(at_iso="2026-05-08T11:00:00+00:00"), None, now)


def test_at_not_due_before_target():
    now = datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc)
    assert not heartbeat.is_action_due(_at(at_iso="2026-05-08T11:00:00+00:00"), None, now)


def test_at_already_fired_skips():
    now = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    last = "2026-05-08T11:30:00+00:00"
    assert not heartbeat.is_action_due(_at(at_iso="2026-05-08T11:00:00+00:00"), last, now)


def test_every_first_run_due():
    assert heartbeat.is_action_due(_every(60), None, datetime.now(timezone.utc))


def test_every_within_interval_skips():
    now = datetime.now(timezone.utc)
    last = (now - timedelta(seconds=10)).isoformat()
    assert not heartbeat.is_action_due(_every(60), last, now)


def test_every_after_interval_due():
    now = datetime.now(timezone.utc)
    last = (now - timedelta(seconds=120)).isoformat()
    assert heartbeat.is_action_due(_every(60), last, now)


def test_cron_helsinki_monday_morning_due():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_mon_0700 = datetime(2026, 5, 11, 7, 0, tzinfo=tz)
    now_utc = helsinki_mon_0700.astimezone(timezone.utc)
    action = _cron(days=("mon", "thu"), time="07:00", tz="Europe/Helsinki")
    assert heartbeat.is_action_due(action, None, now_utc)


def test_cron_skipped_on_other_weekday():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_tue_0700 = datetime(2026, 5, 12, 7, 0, tzinfo=tz)
    now_utc = helsinki_tue_0700.astimezone(timezone.utc)
    action = _cron(days=("mon", "thu"), time="07:00", tz="Europe/Helsinki")
    assert not heartbeat.is_action_due(action, None, now_utc)


def test_cron_skipped_before_schedule_time():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_mon_0600 = datetime(2026, 5, 11, 6, 0, tzinfo=tz)
    now_utc = helsinki_mon_0600.astimezone(timezone.utc)
    action = _cron(days=("mon",), time="07:00", tz="Europe/Helsinki")
    assert not heartbeat.is_action_due(action, None, now_utc)


def test_cron_skipped_when_already_run_today():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_mon_0800 = datetime(2026, 5, 11, 8, 0, tzinfo=tz)
    now_utc = helsinki_mon_0800.astimezone(timezone.utc)
    last_run = datetime(2026, 5, 11, 7, 5, tzinfo=tz).astimezone(timezone.utc).isoformat()
    action = _cron(days=("mon",), time="07:00", tz="Europe/Helsinki")
    assert not heartbeat.is_action_due(action, last_run, now_utc)


# --- active hours ---


def test_active_hours_simple_window():
    now = datetime(2026, 5, 7, 9, 0)
    assert heartbeat._within_active_hours(now, (8, 22))
    assert not heartbeat._within_active_hours(now.replace(hour=23), (8, 22))


def test_active_hours_overnight_window():
    assert heartbeat._within_active_hours(datetime(2026, 5, 7, 23, 0), (22, 6))
    assert heartbeat._within_active_hours(datetime(2026, 5, 7, 5, 0), (22, 6))
    assert not heartbeat._within_active_hours(datetime(2026, 5, 7, 12, 0), (22, 6))


def test_active_hours_none_always_active():
    assert heartbeat._within_active_hours(datetime(2026, 5, 7, 3, 0), None)


# --- run_tick: routing + lifecycle ---


class _StubChannel:
    name = "cli"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def notify(self, scope, text):
        self.calls.append((scope, text))

    def active_scopes(self):
        return ("cli",)


def test_run_tick_sentinel_suppresses_notify(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    ch = _StubChannel()
    channels.register(ch)

    with patch("chat_loop.chat", return_value="HEARTBEAT_OK"):
        replies = asyncio.run(heartbeat.run_tick())
    assert all(r == "" for r in replies.values())
    assert ch.calls == []


def test_run_tick_alert_routes_to_channel(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    ch = _StubChannel()
    channels.register(ch)

    with patch("chat_loop.chat", return_value="ALERT: kettle boiling"):
        replies = asyncio.run(heartbeat.run_tick())
    assert "ALERT: kettle boiling" in replies.values()
    assert ch.calls == [("cli", "ALERT: kettle boiling")]


def test_run_tick_one_shot_archives_after_notify(cfg):
    now = datetime.now(timezone.utc)
    fire_at = (now - timedelta(minutes=1)).isoformat()
    heartbeat.create_action(
        kind=heartbeat.ActionKind.AT,
        prompt="remind kettle",
        channel="cli",
        created_from="cli",
        at=fire_at,
    )
    channels.register(_StubChannel())

    with patch("chat_loop.chat", return_value="ALERT"):
        asyncio.run(heartbeat.run_tick())
    ledger = heartbeat.load_ledger()
    assert ledger.actions == []
    assert len(ledger.completed) == 1
    assert ledger.completed[0]["kind"] == "at"


def test_run_tick_one_shot_kept_when_notify_fails(cfg):
    now = datetime.now(timezone.utc)
    fire_at = (now - timedelta(minutes=1)).isoformat()
    heartbeat.create_action(
        kind=heartbeat.ActionKind.AT,
        prompt="remind kettle",
        channel="cli",
        created_from="cli",
        at=fire_at,
    )

    class _FailChannel:
        name = "cli"

        async def notify(self, scope, text):
            raise RuntimeError("network blew up")

        def active_scopes(self):
            return ("cli",)

    channels.register(_FailChannel())

    with patch("chat_loop.chat", return_value="ALERT"):
        asyncio.run(heartbeat.run_tick())
    ledger = heartbeat.load_ledger()
    assert len(ledger.actions) == 1
    assert ledger.completed == []


def test_run_tick_no_channel_resolvable_keeps_action_active(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="telegram-99999999",
        created_from="cli",
        every=1,
    )
    # No telegram channel registered.
    with patch("chat_loop.chat", return_value="ALERT"):
        replies = asyncio.run(heartbeat.run_tick())
    assert replies == {}
    assert len(heartbeat.load_ledger().actions) == 1


def test_run_tick_skips_outside_active_hours(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    channels.register(_StubChannel())
    with patch.object(heartbeat, "_within_active_hours", return_value=False), \
         patch("chat_loop.chat", return_value="ALERT") as chat_mock:
        replies = asyncio.run(heartbeat.run_tick())
    assert replies == {}
    chat_mock.assert_not_called()


# --- summary ---


def test_summary_includes_action_lines(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="watch high-priority email",
        channel="cli",
        created_from="cli",
        every=1800,
    )
    s = heartbeat.summary()
    assert "every 1800s" in s
    assert "cli" in s


def test_summary_empty(cfg):
    assert heartbeat.summary() == ""


# --- tools ---


def test_create_tool_uses_current_scope(cfg):
    from tools import heartbeat_tools

    token = chat_loop.current_scope.set("telegram-42")
    try:
        result = heartbeat_tools.create_heartbeat_action(
            kind="every",
            prompt="watch the kettle",
            every=300,
        )
    finally:
        chat_loop.current_scope.reset(token)
    assert result["status"] == "created"
    assert result["action"]["channel"] == "telegram-42"
    assert result["action"]["created_from"] == "telegram-42"


def test_create_tool_falls_back_to_default_channel(cfg):
    from tools import heartbeat_tools

    result = heartbeat_tools.create_heartbeat_action(
        kind="every",
        prompt="watch something",
        every=300,
    )
    assert result["status"] == "created"
    assert result["action"]["channel"] == "cli"  # heartbeat_default_channel


def test_create_tool_no_channel_no_default(tmp_path, monkeypatch, clear_env, write_config):
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        vault_path=str(tmp_path / "vault"),
        memory_subdir="Mem",
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfgdir"))
    config.reset_cache()
    (tmp_path / "vault" / "Mem").mkdir(parents=True)
    try:
        from tools import heartbeat_tools

        result = heartbeat_tools.create_heartbeat_action(
            kind="every",
            prompt="watch something",
            every=300,
        )
        assert result["error"] == "no_channel"
    finally:
        config.reset_cache()


def test_list_tool_returns_active_only_by_default(cfg):
    from tools import heartbeat_tools

    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=60,
    )
    out = heartbeat_tools.list_heartbeat_actions()
    assert len(out["active"]) == 1
    assert "completed" not in out


def test_cancel_tool(cfg):
    from tools import heartbeat_tools

    a = heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=60,
    )
    out = heartbeat_tools.cancel_heartbeat_action(a.id)
    assert out["status"] == "cancelled"
    assert heartbeat.load_ledger().actions == []


def test_cancel_tool_unknown_id(cfg):
    from tools import heartbeat_tools

    out = heartbeat_tools.cancel_heartbeat_action("ffffff")
    assert out["status"] == "not_found"


# --- channels routing (preserved) ---


def test_for_scope_routes_telegram():
    class _T:
        name = "telegram"

        async def notify(self, scope, text): pass
        def active_scopes(self): return ()

    channels.register(_T())
    assert channels.for_scope("telegram-123") is not None
    assert channels.for_scope("cli") is None
    channels.CHANNELS.clear()
