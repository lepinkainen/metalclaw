"""known_chats table: Telegram/Discord chat persistence for heartbeat fan-out."""

import json
from types import SimpleNamespace

import history
from frontends import discord as dc
from frontends import telegram as tg


def _tg_update(chat_id=77, username="riku", chat_type="private", title=None):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type, title=title),
        effective_user=SimpleNamespace(id=5, username=username),
    )


def test_touch_creates_then_updates(monkeypatch):
    assert history.known_chat_touch("telegram", "1", {"username": "a"}) is True
    assert history.known_chat_touch("telegram", "1", {"title": "t"}) is False
    (row,) = history.known_chats_all("telegram")
    frontend, chat_id, first_seen, last_seen, meta = row
    assert (frontend, chat_id) == ("telegram", "1")
    assert first_seen <= last_seen
    assert json.loads(meta) == {"username": "a", "title": "t"}  # merged, not replaced
    assert history.known_chat_ids("telegram") == ["1"]
    assert history.known_chat_ids("discord") == []


def test_touch_drops_none_values_and_keeps_corrupt_meta():
    history.known_chats_import("discord", ["9"], {"x": 1})
    with history._LOCK:
        history._conn().execute(
            "UPDATE known_chats SET meta = '{broken' WHERE chat_id = '9'"
        )
        history._conn().commit()
    history.known_chat_touch("discord", "9", {"name": "general", "guild": None})
    (row,) = history.known_chats_all("discord")
    meta = json.loads(row[4])
    assert meta["name"] == "general"
    assert "guild" not in meta
    assert meta["_corrupt_meta"] == "{broken"


def test_forget_and_import_ignore_existing():
    history.known_chats_import("telegram", ["1", "2"], {"migrated_from": "x"})
    history.known_chat_touch("telegram", "1", {"username": "a"})
    assert (
        history.known_chats_import("telegram", ["1", "3"], {"migrated_from": "y"}) == 2
    )
    rows = {r[1]: json.loads(r[4]) for r in history.known_chats_all("telegram")}
    assert set(rows) == {"1", "2", "3"}
    assert rows["1"]["username"] == "a"  # INSERT OR IGNORE left the touched row alone
    assert history.known_chat_forget("telegram", "2") is True
    assert history.known_chat_forget("telegram", "2") is False
    assert history.known_chat_ids("telegram") == ["1", "3"]


def test_telegram_remember_chat_persists_with_meta(monkeypatch):
    monkeypatch.setattr(tg, "_known_chats", set())
    upd = _tg_update(chat_id=77, username="riku")
    tg._remember_chat(77, tg._chat_meta(upd))
    tg._remember_chat(77, tg._chat_meta(_tg_update(chat_id=77, username="riku2")))
    assert tg._known_chats == {77}
    assert tg.known_chat_count() == 1
    (row,) = history.known_chats_all("telegram")
    assert row[1] == "77"
    assert json.loads(row[4]) == {"type": "private", "username": "riku2", "user_id": 5}


def test_telegram_load_known_chats_migrates_legacy_json(monkeypatch, tmp_path):
    path = tg._legacy_chats_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([11, 22]))
    assert tg._load_known_chats() == {11, 22}
    assert not path.exists()
    assert path.with_suffix(".json.bak").exists()
    rows = {r[1]: json.loads(r[4]) for r in history.known_chats_all("telegram")}
    assert rows == {
        "11": {"migrated_from": "telegram_chats.json"},
        "22": {"migrated_from": "telegram_chats.json"},
    }
    # idempotent: no legacy file, same result from the table
    assert tg._load_known_chats() == {11, 22}


def test_telegram_load_skips_bad_ids(monkeypatch):
    history.known_chats_import("telegram", ["5", "oops"], {})
    assert tg._load_known_chats() == {5}


def test_telegram_channel_active_scopes_from_table(monkeypatch):
    monkeypatch.setattr(tg, "_known_chats", set())
    tg._remember_chat(1)
    tg._remember_chat(2)
    ch = tg._TelegramChannel(app=None)  # type: ignore[arg-type]
    assert set(ch.active_scopes()) == {"telegram-1", "telegram-2"}


def test_discord_remember_channel_persists_and_reloads(monkeypatch):
    monkeypatch.setattr(dc, "_known_discord_channels", set())
    msg = SimpleNamespace(
        channel=SimpleNamespace(id=4242, name="general"),
        guild=SimpleNamespace(id=1, name="Guild"),
        author="riku#0",
    )
    dc._remember_channel(4242, dc._channel_meta(msg))
    assert dc._known_discord_channels == {4242}
    (row,) = history.known_chats_all("discord")
    assert row[1] == "4242"
    assert json.loads(row[4]) == {
        "type": "SimpleNamespace",
        "name": "general",
        "guild": "Guild",
        "guild_id": 1,
        "author": "riku#0",
    }
    # survives a "restart": fresh set reloaded from the table
    assert dc._load_known_channels() == {4242}


def test_remember_survives_db_failure(monkeypatch):
    monkeypatch.setattr(tg, "_known_chats", set())

    def boom(*_a, **_k):
        raise RuntimeError("db locked")

    monkeypatch.setattr(history, "known_chat_touch", boom)
    tg._remember_chat(9)
    assert tg._known_chats == {9}
