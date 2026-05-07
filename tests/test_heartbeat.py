import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import channels
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


# --- parse_heartbeat_file ---


def test_parse_frontmatter():
    text = """---
tasks:
  - name: ping
    interval: 30m
    prompt: 'Reply HEARTBEAT_OK.'
---

Some free-form notes about the user's day.
"""
    hb = heartbeat.parse_heartbeat_file(text)
    assert len(hb.tasks) == 1
    assert hb.tasks[0].name == "ping"
    assert hb.tasks[0].interval_seconds == 1800
    assert "free-form" in hb.body


def test_parse_bare_yaml():
    text = """tasks:
  - name: mail
    interval: 1h
    prompt: 'Check mail'
  - name: dice
    interval: 5m
    prompt: 'Roll'
"""
    hb = heartbeat.parse_heartbeat_file(text)
    assert [t.name for t in hb.tasks] == ["mail", "dice"]
    assert hb.tasks[0].interval_seconds == 3600
    assert hb.body == ""


def test_parse_pure_markdown_no_tasks():
    text = "# My checklist\n\n- watch the inbox\n"
    hb = heartbeat.parse_heartbeat_file(text)
    assert hb.tasks == []
    assert "watch the inbox" in hb.body


def test_parse_empty():
    hb = heartbeat.parse_heartbeat_file("")
    assert hb.is_empty()


def test_parse_bad_task_raises():
    with pytest.raises(ValueError):
        heartbeat.parse_heartbeat_file("tasks:\n  - {prompt: 'x'}\n")


# --- state file ---


def test_state_round_trip(cfg):
    assert heartbeat.load_state() == {}
    heartbeat.save_state({"cli::ping": "2026-05-07T12:00:00+00:00"})
    assert heartbeat.load_state() == {"cli::ping": "2026-05-07T12:00:00+00:00"}


def test_state_legacy_flat_format(cfg):
    path = heartbeat._state_path()
    path.write_text(json.dumps({"cli::ping": "2026-05-07T12:00:00+00:00"}))
    assert heartbeat.load_state() == {"cli::ping": "2026-05-07T12:00:00+00:00"}


# --- due-filter ---


def _task(interval=60):
    return heartbeat.HeartbeatTask(name="ping", interval_seconds=interval, prompt="x")


def test_is_due_first_run():
    assert heartbeat.is_due({}, "cli", _task(60), datetime.now(timezone.utc))


def test_is_due_within_interval_skips():
    now = datetime.now(timezone.utc)
    state = {"cli::ping": (now - timedelta(seconds=10)).isoformat()}
    assert not heartbeat.is_due(state, "cli", _task(60), now)


def test_is_due_after_interval():
    now = datetime.now(timezone.utc)
    state = {"cli::ping": (now - timedelta(seconds=120)).isoformat()}
    assert heartbeat.is_due(state, "cli", _task(60), now)


def test_is_due_bad_timestamp_treats_as_due():
    assert heartbeat.is_due({"cli::ping": "not-a-date"}, "cli", _task(60), datetime.now(timezone.utc))


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


# --- discover_scopes ---


def test_discover_scopes(cfg):
    mem = cfg.memory_dir
    (mem / "heartbeat-cli.md").write_text("tasks: []")
    (mem / "heartbeat-telegram-42.md").write_text("tasks: []")
    (mem / "regular-memory.md").write_text("# not a heartbeat")
    assert heartbeat.discover_scopes() == ["cli", "telegram-42"]


# --- run_tick: sentinel suppression + channel routing ---


class _StubChannel:
    name = "cli"

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def notify(self, scope, text):
        self.calls.append((scope, text))

    def active_scopes(self):
        return ("cli",)


def test_run_tick_sentinel_suppresses_notify(cfg):
    (cfg.memory_dir / "heartbeat-cli.md").write_text(
        "tasks:\n  - name: ping\n    interval: 1s\n    prompt: 'reply ok'\n"
    )
    ch = _StubChannel()
    channels.register(ch)

    with patch("bot.chat", return_value="HEARTBEAT_OK"):
        replies = asyncio.run(heartbeat.run_tick())
    assert replies == {"cli": ""}
    assert ch.calls == []


def test_run_tick_alerts_via_channel(cfg):
    (cfg.memory_dir / "heartbeat-cli.md").write_text(
        "tasks:\n  - name: ping\n    interval: 1s\n    prompt: 'reply ok'\n"
    )
    ch = _StubChannel()
    channels.register(ch)

    with patch("bot.chat", return_value="ALERT: something happened"):
        replies = asyncio.run(heartbeat.run_tick())
    assert replies["cli"] == "ALERT: something happened"
    assert ch.calls == [("cli", "ALERT: something happened")]


def test_run_tick_skips_when_no_due_and_no_body(cfg):
    now = datetime.now(timezone.utc)
    (cfg.memory_dir / "heartbeat-cli.md").write_text(
        "tasks:\n  - name: ping\n    interval: 3600\n    prompt: 'x'\n"
    )
    heartbeat.save_state({"cli::ping": now.isoformat()})

    called = {"n": 0}

    def fake_chat(messages):
        called["n"] += 1
        return "HEARTBEAT_OK"

    with patch("bot.chat", side_effect=fake_chat):
        asyncio.run(heartbeat.run_tick(now=now))
    assert called["n"] == 0


def test_run_tick_writes_state(cfg):
    (cfg.memory_dir / "heartbeat-cli.md").write_text(
        "tasks:\n  - name: ping\n    interval: 1s\n    prompt: 'x'\n"
    )
    channels.register(_StubChannel())
    with patch("bot.chat", return_value="HEARTBEAT_OK"):
        asyncio.run(heartbeat.run_tick())
    state = heartbeat.load_state()
    assert "cli::ping" in state


# --- channels routing ---


def test_for_scope_routes_telegram():
    class _T:
        name = "telegram"

        async def notify(self, scope, text): pass
        def active_scopes(self): return ()

    channels.register(_T())
    assert channels.for_scope("telegram-123") is not None
    assert channels.for_scope("cli") is None
    channels.CHANNELS.clear()
