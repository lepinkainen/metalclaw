"""Regression tests for #33: blocking I/O must not run on the event loop thread."""

import asyncio
import threading
from unittest.mock import patch

import pytest

import channels
import config
import heartbeat
from chat_loop import Session, run_turn


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


def _main_thread() -> int:
    return threading.main_thread().ident or 0


def test_run_turn_refreshes_system_prompt_off_the_loop_thread(monkeypatch):
    seen: dict[str, int] = {}

    def fake_refresh(session: Session) -> None:
        seen["refresh"] = threading.get_ident()
        session.system = "refreshed"

    monkeypatch.setattr("chat_loop._refresh_system_prompt", fake_refresh)
    session = Session(system="stale")

    def chat_call() -> str:
        seen["chat"] = threading.get_ident()
        assert session.system == "refreshed", "refresh must precede the chat call"
        assert session.history[-1] == {"role": "user", "content": "hi"}
        return "ok"

    raw, _, clean = asyncio.run(run_turn(session, "hi", chat_call))
    assert (raw, clean) == ("ok", "ok")
    assert seen["refresh"] != _main_thread()
    assert seen["chat"] != _main_thread()


class _Channel:
    name = "cli"

    def active_scopes(self):
        return ("cli",)

    async def notify(self, scope: str, text: str) -> None:
        pass


def test_run_tick_ledger_and_state_io_runs_off_the_loop_thread(cfg, monkeypatch):
    channels.register(_Channel())
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="poll",
        channel="cli",
        created_from="cli",
        every=1,
    )
    threads: dict[str, set[int]] = {}

    def spy(name: str):
        real = getattr(heartbeat, name)

        def wrapped(*a, **k):
            threads.setdefault(name, set()).add(threading.get_ident())
            return real(*a, **k)

        monkeypatch.setattr(heartbeat, name, wrapped)

    for name in ("load_state", "load_ledger", "mark_success", "prune_state"):
        spy(name)

    async def fake_run_action(action, now, chat, build_system_prompt):
        return "all good, notify"

    with patch("heartbeat._run_action", fake_run_action):
        result = asyncio.run(heartbeat.run_tick())

    assert list(result.values()) == ["all good, notify"]
    for name in ("load_state", "load_ledger", "mark_success", "prune_state"):
        assert name in threads, f"{name} never called"
        assert _main_thread() not in threads[name], f"{name} ran on the loop thread"
