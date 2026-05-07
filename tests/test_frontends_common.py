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
    memory.current_scope.set("test-scope")
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
    memory.current_scope.set("test-scope")
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


def test_run_forget_match_removes_entry(vault):
    memory.set_preference("tone", "terse")
    send, captured = _send_capture()
    asyncio.run(common.run_forget(send, "tone"))
    assert captured == ["forgot entry matching 'tone'"]
    assert memory.load().preferences == {}


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
        common.run_big(send, contextlib.nullcontext(), [], "test-scope", "")
    )
    assert captured == ["usage: /big <query>"]


def test_run_big_disabled_when_escalation_off(vault):
    send, captured = _send_capture()
    asyncio.run(
        common.run_big(send, contextlib.nullcontext(), [], "test-scope", "hi")
    )
    assert any("escalation disabled" in c for c in captured)


def test_run_big_routes_through_escalation(vault_with_escalation, monkeypatch):
    monkeypatch.setattr(common, "chat_via_escalation", lambda msgs: "cloud reply")

    messages: list[dict] = [{"role": "system", "content": "x"}]
    send, captured = _send_capture()
    asyncio.run(
        common.run_big(
            send, contextlib.nullcontext(), messages, "test-scope", "hi"
        )
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
        common.run_big(
            send, contextlib.nullcontext(), messages, "test-scope", "hi"
        )
    )

    assert all(m.get("role") != "user" for m in messages)
    assert any("Error: boom" in c for c in captured)


# --- run_onboard_start / run_onboard_answer ---


def test_run_onboard_start_fresh(vault):
    state: dict[int, int] = {}
    send, captured = _send_capture()
    asyncio.run(common.run_onboard_start(send, state, 42))
    assert state == {42: 0}
    first_question = common.ONBOARDING_STEPS[0][1]
    assert any(first_question in c for c in captured)


def test_run_onboard_start_already_onboarded(vault):
    memory.set_preference("role", "engineer")
    state: dict[int, int] = {}
    send, captured = _send_capture()
    asyncio.run(common.run_onboard_start(send, state, 42))
    assert state == {}
    assert any("Already onboarded" in c for c in captured)


def test_run_onboard_answer_advances_step(vault):
    state = {42: 0}
    sessions: dict[int, list[dict]] = {}
    send, captured = _send_capture()
    asyncio.run(
        common.run_onboard_answer(send, state, sessions, 42, "engineer")
    )
    assert state == {42: 1}
    assert memory.load().preferences == {"role": "engineer"}


def test_run_onboard_answer_skip_advances_without_saving(vault):
    state = {42: 0}
    sessions: dict[int, list[dict]] = {}
    send, captured = _send_capture()
    asyncio.run(common.run_onboard_answer(send, state, sessions, 42, "-"))
    assert state == {42: 1}
    assert memory.load().preferences == {}


def test_run_onboard_answer_interests_wrapped_in_wikilinks(vault):
    interests_step = next(
        i for i, (k, _) in enumerate(common.ONBOARDING_STEPS) if k == "interests"
    )
    state = {42: interests_step}
    sessions: dict[int, list[dict]] = {}
    send, captured = _send_capture()
    asyncio.run(
        common.run_onboard_answer(send, state, sessions, 42, "python, rust")
    )
    saved = memory.load().preferences["interests"]
    assert "[[python]]" in saved
    assert "[[rust]]" in saved


def test_run_onboard_answer_finishes_and_clears_session(vault):
    last_step = len(common.ONBOARDING_STEPS) - 1
    state = {42: last_step}
    sessions: dict[int, list[dict]] = {42: [{"role": "system", "content": "x"}]}
    send, captured = _send_capture()
    asyncio.run(
        common.run_onboard_answer(send, state, sessions, 42, "Helsinki")
    )
    assert 42 not in state
    assert 42 not in sessions
    assert any("Onboarding done" in c for c in captured)
