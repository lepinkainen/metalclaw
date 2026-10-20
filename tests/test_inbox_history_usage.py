import asyncio
import logging
from datetime import UTC, datetime, timedelta

import pytest

import config
import heartbeat
import history
from frontends import common
from tools.conversation_history import search_conversation_history
from tools.inbox import append_to_inbox

# --- inbox ---


@pytest.fixture
def vault(cfg_file, write_config, tmp_path):
    write_config(cfg_file)
    root = tmp_path / "vault"
    root.mkdir()
    config.reset_cache()
    return root


def test_inbox_appends_timestamped_entries(vault):
    append_to_inbox("buy milk")
    append_to_inbox("line one\nline two")
    lines = (vault / "INBOX.md").read_text().splitlines()
    assert lines[0].startswith("- 20") and lines[0].endswith("— buy milk")
    assert lines[1].endswith("— line one") and lines[2] == "  line two"
    assert [p.name for p in vault.iterdir()] == ["INBOX.md"]


def test_inbox_rejects_empty_and_missing_vault(vault, tmp_path):
    with pytest.raises(ValueError):
        append_to_inbox("  ")
    vault.rmdir()
    with pytest.raises(RuntimeError):
        append_to_inbox("x")


# --- conversation history ---


def test_history_search_newest_first_across_surfaces():
    history.record_message("20260101T000000", "user", "Hello CLI 100%")
    history.record_message("telegram-5", "user", "hello telegram")
    history.record_message("discord-9", "assistant", "nothing here")
    out = search_conversation_history("HELLO")["matches"]
    assert [m["surface"] for m in out] == ["telegram-5", "cli"]
    assert search_conversation_history("100%")["matches"][0]["surface"] == "cli"
    assert search_conversation_history("%")["matches"][0]["content"] == "Hello CLI 100%"
    assert len(search_conversation_history("hello", limit=1)["matches"]) == 1


# --- usage ---


def test_usage_summary_and_format():
    history.record_usage("ollama", "gemma", 10, 5, None)
    history.record_usage("litellm", "bedrock/x", 100, 50, 0.01)
    history.record_usage("litellm", "bedrock/x", 100, 50, None)
    rows = history.usage_since((datetime.now(UTC) - timedelta(days=1)).isoformat())
    assert rows[0][:2] == ("litellm", "bedrock/x") and rows[0][2] == 2
    assert (
        history.usage_since((datetime.now(UTC) + timedelta(days=1)).isoformat()) == []
    )
    text = common.format_usage()
    assert "ollama/gemma: 1 call(s), 10 in / 5 out, cost n/a" in text
    assert "$0.0100 (partial)" in text


# --- status ---


def test_status_lists_warnings_and_next_due(cfg_file, write_config):
    write_config(cfg_file)
    heartbeat.create_action(
        kind=heartbeat.ActionKind.EVERY,
        prompt="ping",
        channel="cli",
        created_from=None,
        every=3600,
    )
    common.recent_warnings.records.clear()
    log = logging.getLogger("t")
    log.addHandler(common.recent_warnings)
    try:
        log.warning("boom %d", 7)
    finally:
        log.removeHandler(common.recent_warnings)
    text = common.format_status()
    assert "provider: ollama" in text and "boom 7" in text
    assert "[every] next:" in text


# --- debounce ---


def test_debouncer_joins_and_serialises():
    async def run():
        got = []

        async def flush(key, text, ctx):
            got.append((key, text, ctx))

        d = common.Debouncer(flush)
        d.add(1, "a", "c1", 0.05)
        d.add(1, "b", "c2", 0.05)
        d.add(2, "z", "c3", 0.05)
        await asyncio.sleep(0.15)
        return got

    got = asyncio.run(run())
    assert sorted(got) == [(1, "a\nb", "c2"), (2, "z", "c3")]


def test_debouncer_message_during_flush_starts_new_turn():
    async def run():
        got, gate = [], asyncio.Event()

        async def flush(key, text, ctx):
            got.append(text)
            if text == "a":
                await gate.wait()

        d = common.Debouncer(flush)
        d.add(1, "a", None, 0.02)
        await asyncio.sleep(0.06)  # "a" flushing, blocked
        d.add(1, "b", None, 0.02)
        await asyncio.sleep(0.06)  # "b" flushed concurrently, "a" not cancelled
        gate.set()
        await asyncio.sleep(0.02)
        return got

    assert asyncio.run(run()) == ["a", "b"]


def test_telegram_debounces_messages_into_one_turn(cfg_file, write_config, monkeypatch):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from frontends import telegram as tg

    write_config(cfg_file, message_debounce_seconds=0.05)
    config.reset_cache()
    turns, replies = [], []

    async def fake_run_turn(messages, text, call):
        turns.append(text)
        return text, "", "ok"

    async def fake_reply(update, text):
        replies.append((update.n, text))

    @asynccontextmanager
    async def fake_typing(*_):
        yield

    monkeypatch.setattr(tg, "run_turn", fake_run_turn)
    monkeypatch.setattr(tg, "_tg_reply", fake_reply)
    monkeypatch.setattr(tg, "_typing", fake_typing)
    monkeypatch.setattr(tg, "_tool_status_for", lambda *_: None)
    monkeypatch.setattr(tg, "_remember_chat", lambda *_a, **_k: None)
    monkeypatch.setattr(tg, "build_system_prompt", lambda _: "sys")

    def upd(n, text):
        return SimpleNamespace(
            n=n,
            message=SimpleNamespace(text=text),
            effective_chat=SimpleNamespace(id=77),
            effective_user=None,
        )

    async def run():
        ctx = SimpleNamespace(bot=None)
        await tg._telegram_handle_message(upd(1, "Remind me to buy this"), ctx)
        await tg._telegram_handle_message(upd(2, "https://store.example/game"), ctx)
        await asyncio.sleep(0.2)

    asyncio.run(run())
    assert turns == ["Remind me to buy this\nhttps://store.example/game"]
    assert replies == [(2, "ok")]
    rows = history.search_messages("", 10)
    assert [(r[1], r[2]) for r in rows] == [
        ("telegram-77", "assistant"),
        ("telegram-77", "user"),
    ]
