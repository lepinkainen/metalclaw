import asyncio
import contextlib

import pytest

import config
import memory
from frontends import common


def _send_capture():
    captured: list[str] = []

    async def send(text: str) -> None:
        captured.append(text)

    return send, captured


@pytest.fixture
def vault(tmp_path, monkeypatch, clear_env, write_config):
    vault_dir = tmp_path / "vault"
    cfg_path = tmp_path / "config.yaml"
    write_config(cfg_path, vault_path=str(vault_dir), memory_subdir="Mem")
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    config.reset_cache()
    (vault_dir / "Mem").mkdir(parents=True)
    yield vault_dir / "Mem"
    config.reset_cache()


@pytest.fixture
def vault_with_escalation(tmp_path, monkeypatch, clear_env, write_config):
    vault_dir = tmp_path / "vault"
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        vault_path=str(vault_dir),
        memory_subdir="Mem",
        escalation_enabled=True,
        escalation_provider="anthropic",
        anthropic_api_key="sk-ant-test",
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    config.reset_cache()
    (vault_dir / "Mem").mkdir(parents=True)
    yield vault_dir / "Mem"
    config.reset_cache()


# --- run_remember ---


def test_run_remember_saves(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_remember(send, "tone=terse"))
    assert captured == ["saved tone=terse"]
    assert memory.load().preferences == {"tone": "terse"}


def test_run_remember_missing_equals(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_remember(send, "no-equals"))
    assert captured == ["usage: /remember <key>=<value>"]


def test_run_remember_empty_value(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_remember(send, "key="))
    assert captured == ["usage: /remember <key>=<value>"]


def test_run_remember_empty_key(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_remember(send, "=val"))
    assert captured == ["usage: /remember <key>=<value>"]


# --- run_forget ---


def test_run_forget_no_match(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_forget(send, "missing"))
    assert captured == ["no entry matched 'missing'"]


def test_run_forget_unique_match_removes_and_echoes_entry(vault):
    memory.set_preference("tone", "terse")
    send, captured = _send_capture()
    asyncio.run(common.run_forget(send, "tone"))
    assert captured == ["forgot: [pref] **tone**: terse"]
    assert memory.load().preferences == {}


def test_run_forget_ambiguous_lists_candidates_and_keeps_all(vault):
    memory.set_preference("role", "engineer")
    memory.set_preference("tone", "terse")
    memory.add_fact("drinks coffee")
    send, captured = _send_capture()
    asyncio.run(common.run_forget(send, "e"))
    assert len(captured) == 1
    out = captured[0]
    assert "matches" in out
    assert "[pref] **role**: engineer" in out
    assert "refine matcher" in out
    mem = memory.load()
    assert mem.preferences == {"role": "engineer", "tone": "terse"}
    assert mem.facts == ["drinks coffee"]


def test_run_forget_empty_args_shows_usage(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_forget(send, ""))
    assert captured == ["usage: /forget <substring>"]


# --- run_memory ---


def test_run_memory_returns_render(vault):
    memory.set_preference("k", "v")
    send, captured = _send_capture()
    asyncio.run(common.run_memory(send))
    assert len(captured) == 1
    assert "k" in captured[0]
    assert "v" in captured[0]


# --- run_heartbeat ---


def test_run_heartbeat_status_no_checklist(vault):
    send, captured = _send_capture()
    asyncio.run(common.run_heartbeat(send, "test-scope", ""))
    out = "\n".join(captured)
    assert "heartbeat enabled=" in out
    assert "no checklist" in out


def test_run_heartbeat_status_lists_tasks(vault):
    path = vault / "heartbeat-test-scope.md"
    path.write_text(
        "---\n"
        "tasks:\n"
        "  - name: ping\n"
        "    interval: 1h\n"
        "    prompt: ping\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    send, captured = _send_capture()
    asyncio.run(common.run_heartbeat(send, "test-scope", ""))
    out = "\n".join(captured)
    assert "ping" in out
    assert "every 3600s" in out


def test_run_heartbeat_warn_no_discord_channel(vault):
    send, captured = _send_capture()
    asyncio.run(
        common.run_heartbeat(send, "test-scope", "", warn_no_discord_channel=True)
    )
    out = "\n".join(captured)
    assert "no discord_heartbeat_channel" in out


def test_run_heartbeat_parse_error_reported(vault):
    path = vault / "heartbeat-test-scope.md"
    path.write_text(
        "---\n"
        "tasks:\n"
        "  - {name: 'broken', no_interval: yes}\n"
        "---\n",
        encoding="utf-8",
    )
    send, captured = _send_capture()
    asyncio.run(common.run_heartbeat(send, "test-scope", ""))
    out = "\n".join(captured)
    assert "parse error" in out


# --- run_big ---


def test_run_big_empty_query(vault):
    send, captured = _send_capture()
    asyncio.run(
        common.run_big(send, contextlib.nullcontext(), [], "")
    )
    assert captured == ["usage: /big <query>"]


def test_run_big_disabled_when_escalation_off(vault):
    send, captured = _send_capture()
    asyncio.run(
        common.run_big(send, contextlib.nullcontext(), [], "hi")
    )
    assert any("escalation disabled" in c for c in captured)


def test_run_big_routes_through_escalation(vault_with_escalation, monkeypatch):
    monkeypatch.setattr(common, "chat_via_escalation", lambda msgs: "cloud reply")

    messages: list[dict] = [{"role": "system", "content": "x"}]
    send, captured = _send_capture()
    asyncio.run(
        common.run_big(send, contextlib.nullcontext(), messages, "hi")
    )

    assert messages[-2] == {"role": "user", "content": "hi"}
    assert messages[-1] == {"role": "assistant", "content": "cloud reply"}
    assert captured[-1] == "cloud reply"


def test_run_big_rolls_back_on_exception(vault_with_escalation, monkeypatch):
    def boom(_msgs):
        raise RuntimeError("boom")

    monkeypatch.setattr(common, "chat_via_escalation", boom)

    messages: list[dict] = [{"role": "system", "content": "x"}]
    send, captured = _send_capture()
    asyncio.run(
        common.run_big(send, contextlib.nullcontext(), messages, "hi")
    )

    assert all(m.get("role") != "user" for m in messages)
    assert any("Error: boom" in c for c in captured)
