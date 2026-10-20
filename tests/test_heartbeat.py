import asyncio
import json
from datetime import UTC, datetime, timedelta
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


# --- state (heartbeat_state table in history.db) ---


def _state_rows():
    import history

    return history.heartbeat_state_all()


def test_state_empty_by_default(cfg):
    assert heartbeat.load_state() == {}
    assert heartbeat.get_state("nope") is None


def test_mark_failure_and_success_round_trip(cfg):
    a = _every(action_id="abc123")
    now = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    assert heartbeat.mark_failure(a, now, "boom") == 1
    assert heartbeat.mark_failure(a, now + timedelta(minutes=1), "boom again") == 2
    st = heartbeat.get_state("abc123")
    assert st is not None
    assert st.last_run is None
    assert st.failure_count == 2
    assert st.last_failed == (now + timedelta(minutes=1)).isoformat()
    assert st.retry_at() == now + timedelta(minutes=1, seconds=120)
    assert st.meta["last_outcome"] == "failed"
    assert st.meta["last_error"] == "boom again"
    assert st.meta["kind"] == "every" and st.meta["channel"] == "cli"
    assert st.updated_at

    heartbeat.mark_success(a, now + timedelta(minutes=5), last_outcome="notified")
    st = heartbeat.get_state("abc123")
    assert st is not None
    assert st.last_run == (now + timedelta(minutes=5)).isoformat()
    assert st.failure_count == 0 and st.last_failed is None
    assert st.retry_at() is None
    # meta is merged, not replaced: the old error key survives, outcome updated
    assert st.meta["last_outcome"] == "notified"
    assert st.meta["last_error"] == "boom again"


def test_state_meta_survives_corrupt_json(cfg):
    import history

    history.heartbeat_state_import(
        [("zz", None, 0, None, "2026-01-01T00:00:00+00:00", "{not json")]
    )
    st = heartbeat.get_state("zz")
    assert st is not None and "_corrupt_meta" in st.meta
    heartbeat.mark_failure(_every(action_id="zz"), datetime.now(UTC), "x")
    st = heartbeat.get_state("zz")
    assert st is not None and st.meta["last_error"] == "x"


def test_forget_and_prune_state(cfg):
    now = datetime.now(UTC)
    for aid in ("a1", "b2", "c3"):
        heartbeat.mark_success(_every(action_id=aid), now)
    heartbeat.forget_state("a1")
    assert set(heartbeat.load_state()) == {"b2", "c3"}
    assert heartbeat.prune_state({"c3"}) == 1
    assert set(heartbeat.load_state()) == {"c3"}
    assert heartbeat.prune_state(set()) == 1
    assert heartbeat.load_state() == {}


def test_backoff_seconds_doubles_and_caps(cfg):
    base = config.get_config().heartbeat_interval_seconds  # 60 in fixture
    assert heartbeat.backoff_seconds(1) == base
    assert heartbeat.backoff_seconds(2) == base * 2
    assert heartbeat.backoff_seconds(3) == base * 4
    assert heartbeat.backoff_seconds(100) == 3600


@pytest.mark.parametrize(
    "payload",
    [
        {"abc123": "2026-05-07T12:00:00+00:00"},
        {"version": 1, "last_run": {"abc123": "2026-05-07T12:00:00+00:00"}},
        {
            "version": 2,
            "last_run": {"abc123": "2026-05-07T12:00:00+00:00"},
            "failures": {
                "def456": {"count": 2, "last_failed": "2026-05-07T12:30:00+00:00"},
                "bad": {"count": "x"},
                "zero": {"count": 0, "last_failed": "2026-05-07T12:30:00+00:00"},
            },
        },
    ],
)
def test_legacy_json_state_is_migrated(cfg, payload):
    path = heartbeat._legacy_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    heartbeat.reset_state_migration_flag()

    st = heartbeat.load_state()
    assert st["abc123"].last_run == "2026-05-07T12:00:00+00:00"
    assert st["abc123"].meta == {"migrated_from": "heartbeat_state.json"}
    if "failures" in payload:
        assert st["def456"].failure_count == 2
        assert st["def456"].last_failed == "2026-05-07T12:30:00+00:00"
        assert st["def456"].last_run is None
        assert "bad" not in st and "zero" not in st
    assert not path.exists()
    assert path.with_suffix(".json.bak").exists()
    # second load must not re-import
    heartbeat.reset_state_migration_flag()
    assert set(heartbeat.load_state()) == set(st)


def test_cancel_forgets_state(cfg):
    a = heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    heartbeat.mark_success(a, datetime.now(UTC))
    assert a.id in heartbeat.load_state()
    heartbeat.cancel(a.id)
    assert a.id not in heartbeat.load_state()


def test_generate_id_skips_ids_with_state(cfg, monkeypatch):
    now = datetime.now(UTC)
    heartbeat.mark_success(_every(action_id="aaaaaa"), now)
    tokens = iter(["aaaaaa", "bbbbbb"])
    monkeypatch.setattr(heartbeat.secrets, "token_hex", lambda n: next(tokens))
    a = heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    assert a.id == "bbbbbb"


def test_run_tick_prunes_orphan_state(cfg):
    heartbeat.mark_success(_every(action_id="orphan"), datetime.now(UTC))
    channels.register(_StubChannel())
    with patch("chat_loop.chat", return_value="HEARTBEAT_OK"):
        asyncio.run(heartbeat.run_tick())
    assert "orphan" not in heartbeat.load_state()


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
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    assert heartbeat.is_action_due(_at(at_iso="2026-05-08T11:00:00+00:00"), None, now)


def test_at_not_due_before_target():
    now = datetime(2026, 5, 8, 10, 0, tzinfo=UTC)
    assert not heartbeat.is_action_due(
        _at(at_iso="2026-05-08T11:00:00+00:00"), None, now
    )


def test_at_already_fired_skips():
    now = datetime(2026, 5, 8, 12, 0, tzinfo=UTC)
    last = "2026-05-08T11:30:00+00:00"
    assert not heartbeat.is_action_due(
        _at(at_iso="2026-05-08T11:00:00+00:00"), last, now
    )


def test_every_first_run_due():
    assert heartbeat.is_action_due(_every(60), None, datetime.now(UTC))


def test_every_within_interval_skips():
    now = datetime.now(UTC)
    last = (now - timedelta(seconds=10)).isoformat()
    assert not heartbeat.is_action_due(_every(60), last, now)


def test_every_after_interval_due():
    now = datetime.now(UTC)
    last = (now - timedelta(seconds=120)).isoformat()
    assert heartbeat.is_action_due(_every(60), last, now)


def test_cron_helsinki_monday_morning_due():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_mon_0700 = datetime(2026, 5, 11, 7, 0, tzinfo=tz)
    now_utc = helsinki_mon_0700.astimezone(UTC)
    action = _cron(days=("mon", "thu"), time="07:00", tz="Europe/Helsinki")
    assert heartbeat.is_action_due(action, None, now_utc)


def test_cron_skipped_on_other_weekday():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_tue_0700 = datetime(2026, 5, 12, 7, 0, tzinfo=tz)
    now_utc = helsinki_tue_0700.astimezone(UTC)
    action = _cron(days=("mon", "thu"), time="07:00", tz="Europe/Helsinki")
    assert not heartbeat.is_action_due(action, None, now_utc)


def test_cron_skipped_before_schedule_time():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_mon_0600 = datetime(2026, 5, 11, 6, 0, tzinfo=tz)
    now_utc = helsinki_mon_0600.astimezone(UTC)
    action = _cron(days=("mon",), time="07:00", tz="Europe/Helsinki")
    assert not heartbeat.is_action_due(action, None, now_utc)


def test_cron_skipped_when_already_run_today():
    tz = ZoneInfo("Europe/Helsinki")
    helsinki_mon_0800 = datetime(2026, 5, 11, 8, 0, tzinfo=tz)
    now_utc = helsinki_mon_0800.astimezone(UTC)
    last_run = datetime(2026, 5, 11, 7, 5, tzinfo=tz).astimezone(UTC).isoformat()
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


@pytest.mark.parametrize(
    ("reply", "sent"),
    [
        ("HEARTBEAT_OK", None),
        ("No messages require action.\n\nHEARTBEAT_OK", None),
        ("**HEARTBEAT_OK**", None),
        ("x" * 400 + "\nHEARTBEAT_OK", "x" * 400),
    ],
)
def test_run_tick_sentinel_suppresses_notify(cfg, reply, sent):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    ch = _StubChannel()
    channels.register(ch)

    with patch("chat_loop.chat", return_value=reply):
        asyncio.run(heartbeat.run_tick())
    assert ch.calls == ([] if sent is None else [("cli", sent)])


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
    now = datetime.now(UTC)
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
    now = datetime.now(UTC)
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


def test_run_tick_empty_reply_keeps_one_shot_active(cfg):
    now = datetime.now(UTC)
    fire_at = (now - timedelta(minutes=1)).isoformat()
    heartbeat.create_action(
        kind=heartbeat.ActionKind.AT,
        prompt="remind kettle",
        channel="cli",
        created_from="cli",
        at=fire_at,
    )
    ch = _StubChannel()
    channels.register(ch)

    with patch("chat_loop.chat", return_value=""):
        replies = asyncio.run(heartbeat.run_tick())
    assert replies == {}
    assert ch.calls == []
    ledger = heartbeat.load_ledger()
    assert len(ledger.actions) == 1
    assert ledger.completed == []
    st = heartbeat.load_state()
    assert [v.failure_count for v in st.values()] == [1]
    assert all(v.last_run is None for v in st.values())


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
    with (
        patch.object(heartbeat, "_within_active_hours", return_value=False),
        patch("chat_loop.chat", return_value="ALERT") as chat_mock,
    ):
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


def test_create_tool_no_channel_no_default(
    tmp_path, monkeypatch, clear_env, write_config
):
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

        async def notify(self, scope, text):
            pass

        def active_scopes(self):
            return ()

    channels.register(_T())
    assert channels.for_scope("telegram-123") is not None
    assert channels.for_scope("cli") is None
    channels.CHANNELS.clear()


def test_parse_iso_naive_is_local_time():
    naive = "2026-10-20T16:00:00"
    dt = heartbeat.parse_iso(naive)
    assert dt.tzinfo is not None
    local = datetime(2026, 10, 20, 16, 0).astimezone()
    assert dt == local
    assert dt.hour == 16 and dt.utcoffset() == local.utcoffset()


def test_parse_iso_aware_preserved():
    dt = heartbeat.parse_iso("2026-10-20T16:00:00+03:00")
    assert dt.utcoffset() == timedelta(hours=3)
    assert dt.hour == 16


def test_format_now_includes_offset():
    s = heartbeat.format_now(datetime(2026, 10, 20, 15, 30).astimezone())
    assert s.startswith("2026-10-20 15:30 ")
    assert "(UTC+" in s or "(UTC-" in s


def test_system_prompt_clock_has_offset(cfg):
    from chat_loop import build_system_prompt, format_now

    prompt = build_system_prompt(format_now())
    assert "(UTC+" in prompt or "(UTC-" in prompt


def _fail_channel():
    class _FailChannel:
        name = "cli"
        calls = 0

        async def notify(self, scope, text):
            type(self).calls += 1
            raise RuntimeError("gateway down")

        def active_scopes(self):
            return ("cli",)

    return _FailChannel


def test_run_tick_failed_delivery_backs_off(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    fc = _fail_channel()
    channels.register(fc())
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    interval = config.get_config().heartbeat_interval_seconds

    with patch("chat_loop.chat", return_value="ALERT") as chat:
        asyncio.run(heartbeat.run_tick(now=t0))  # fails: failure #1
        assert chat.call_count == 1
        st = heartbeat.load_state()
        assert [v.failure_count for v in st.values()] == [1]

        # still inside backoff window → no model call
        asyncio.run(heartbeat.run_tick(now=t0 + timedelta(seconds=interval // 2)))
        assert chat.call_count == 1

        # after one interval → retried, fails again: failure #2
        asyncio.run(heartbeat.run_tick(now=t0 + timedelta(seconds=interval)))
        assert chat.call_count == 2
        # backoff now 2*interval → skipped at +interval, runs at +2*interval
        asyncio.run(heartbeat.run_tick(now=t0 + timedelta(seconds=2 * interval)))
        assert chat.call_count == 2
        asyncio.run(heartbeat.run_tick(now=t0 + timedelta(seconds=3 * interval)))
        assert chat.call_count == 3
    assert fc.calls == 3


def test_run_tick_success_clears_backoff(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    fc = _fail_channel()
    channels.register(fc())
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    interval = config.get_config().heartbeat_interval_seconds
    with patch("chat_loop.chat", return_value="ALERT"):
        asyncio.run(heartbeat.run_tick(now=t0))
    assert [v.failure_count for v in heartbeat.load_state().values()] == [1]

    ok = _StubChannel()
    channels.register(ok)
    with patch("chat_loop.chat", return_value="ALERT"):
        asyncio.run(heartbeat.run_tick(now=t0 + timedelta(seconds=interval)))
    st = heartbeat.load_state()
    assert [v.failure_count for v in st.values()] == [0]
    assert ok.calls == [("cli", "ALERT")]
    assert [v.last_run for v in st.values()] == [
        (t0 + timedelta(seconds=interval)).isoformat()
    ]
    (row,) = st.values()
    assert row.meta["last_outcome"] == "notified"
    assert row.meta["target"] == "cli"
    assert row.meta["last_error"].startswith("channel notify failed")


def test_run_tick_sentinel_clears_backoff(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    channels.register(_StubChannel())
    t0 = datetime(2026, 5, 7, 12, 0, tzinfo=UTC)
    interval = config.get_config().heartbeat_interval_seconds
    with patch("chat_loop.chat", return_value=""):
        asyncio.run(heartbeat.run_tick(now=t0))
    assert [v.failure_count for v in heartbeat.load_state().values()] == [1]
    with patch("chat_loop.chat", return_value="HEARTBEAT_OK"):
        asyncio.run(heartbeat.run_tick(now=t0 + timedelta(seconds=interval)))
    st = heartbeat.load_state()
    assert [v.failure_count for v in st.values()] == [0]
    assert [v.meta["last_outcome"] for v in st.values()] == ["suppressed"]


def test_status_shows_backoff(cfg):
    from frontends import common

    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    channels.register(_fail_channel()())
    with patch("chat_loop.chat", return_value="ALERT"):
        asyncio.run(heartbeat.run_tick())
    text = common.format_status()
    assert "1 failure, backing off" in text


# --- single-flight ticks (/heartbeat run vs scheduler) ---


def test_run_tick_skips_while_another_tick_runs(cfg):
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    ch = _StubChannel()
    channels.register(ch)
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_chat(messages, **_):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ALERT"

    async def fake_run_action(action, now, chat, build_system_prompt):
        return await slow_chat(None)

    async def scenario():
        with patch("heartbeat._run_action", fake_run_action):
            first = asyncio.create_task(heartbeat.run_tick())
            await started.wait()
            assert heartbeat.is_tick_running()
            second = await heartbeat.run_tick()  # overlapping → skipped
            assert second == {}
            assert heartbeat.spawn_tick() is None
            release.set()
            result = await first
        assert not heartbeat.is_tick_running()
        return result

    result = asyncio.run(scenario())
    assert calls == 1
    assert list(result.values()) == ["ALERT"]
    assert ch.calls == [("cli", "ALERT")]


def test_tick_flag_cleared_after_crash(cfg):
    async def boom(**_):
        raise RuntimeError("tick blew up")

    with (
        patch("heartbeat._run_tick_locked", boom),
        pytest.raises(RuntimeError),
    ):
        asyncio.run(heartbeat.run_tick())
    assert not heartbeat.is_tick_running()


def test_spawn_tick_keeps_reference_until_done(cfg):
    channels.register(_StubChannel())

    async def scenario():
        with patch("chat_loop.chat", return_value="HEARTBEAT_OK"):
            task = heartbeat.spawn_tick()
            assert task is not None
            assert task in heartbeat._background_ticks
            await task
        assert task not in heartbeat._background_ticks
        assert not heartbeat.is_tick_running()

    asyncio.run(scenario())
