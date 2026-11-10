"""Per-frontend sender allowlists (telegram_allowed_users / discord_allowed_users)."""

import asyncio
from types import SimpleNamespace

import pytest

import config
import history
from chat_loop import Session
from frontends import common
from frontends import discord as dc
from frontends import telegram as tg

ALLOWED = 5
STRANGER = 666


@pytest.fixture
def allowlisted(cfg_file, write_config):
    write_config(
        cfg_file,
        message_debounce_seconds=0,
        telegram_allowed_users=[ALLOWED],
        discord_allowed_users=[ALLOWED],
        discord_chat_channels=[4242],
    )
    config.reset_cache()


@pytest.fixture
def recorder(monkeypatch):
    """Record every path that would start a turn, reply, or load a session."""
    calls: dict[str, list] = {"turn": [], "cmd": [], "reply": [], "load": []}

    async def tg_turn(chat_id, text, ctx):
        calls["turn"].append(("telegram", chat_id, text))

    async def tg_cmd(update, cmd, args, bot=None):
        calls["cmd"].append(("telegram", cmd))

    async def tg_reply(update, text):
        calls["reply"].append(("telegram", text))

    async def dc_turn(channel_id, text, message):
        calls["turn"].append(("discord", channel_id, text))

    async def dc_cmd(message, cmd, args):
        calls["cmd"].append(("discord", cmd))

    def load(scope):
        calls["load"].append(scope)
        return Session(system="", scope=scope)

    monkeypatch.setattr(tg, "_run_text_turn", tg_turn)
    monkeypatch.setattr(tg, "_telegram_dispatch_command", tg_cmd)
    monkeypatch.setattr(tg, "_tg_reply", tg_reply)
    monkeypatch.setattr(tg, "_known_chats", set())
    monkeypatch.setattr(dc, "_run_text_turn", dc_turn)
    monkeypatch.setattr(dc, "_discord_dispatch_command", dc_cmd)
    monkeypatch.setattr(dc, "_known_discord_channels", set())
    monkeypatch.setattr(Session, "load", staticmethod(load))
    return calls


def _tg_update(user_id, text="hello"):
    return SimpleNamespace(
        message=SimpleNamespace(text=text),
        effective_chat=SimpleNamespace(id=77, type="private", title=None),
        effective_user=SimpleNamespace(id=user_id, username="someone"),
    )


def _dc_message(user_id, text="hello"):
    return SimpleNamespace(
        author=SimpleNamespace(id=user_id, bot=False),
        content=text,
        channel=SimpleNamespace(id=4242, name="general"),
        guild=SimpleNamespace(id=1, name="Guild"),
        mentions=[],
        reference=None,
    )


_DC_CLIENT = SimpleNamespace(user=SimpleNamespace(id=999))
_CTX = SimpleNamespace(bot=None, args=[])


# --- helper ---------------------------------------------------------------


def test_is_allowed_user_empty_list_denies_everyone():
    assert not common.is_allowed_user(common.TELEGRAM, ALLOWED)
    assert not common.is_allowed_user(common.DISCORD, ALLOWED)


def test_is_allowed_user_checks_the_right_frontend(cfg_file, write_config):
    write_config(cfg_file, telegram_allowed_users=[1], discord_allowed_users=[2])
    config.reset_cache()
    assert common.is_allowed_user(common.TELEGRAM, 1)
    assert not common.is_allowed_user(common.TELEGRAM, 2)
    assert common.is_allowed_user(common.DISCORD, 2)
    assert not common.is_allowed_user(common.DISCORD, 1)
    assert not common.is_allowed_user(common.TELEGRAM, None)


# --- Telegram -------------------------------------------------------------


def test_telegram_message_from_stranger_is_ignored(allowlisted, recorder, caplog):
    asyncio.run(tg._telegram_handle_message(_tg_update(STRANGER), _CTX))
    assert recorder == {"turn": [], "cmd": [], "reply": [], "load": []}
    assert history.known_chats_all("telegram") == []
    assert tg._known_chats == set()
    assert f"unauthorised user id={STRANGER}" in caplog.text


def test_telegram_slash_text_from_stranger_is_ignored(allowlisted, recorder):
    asyncio.run(tg._telegram_handle_message(_tg_update(STRANGER, "/status"), _CTX))
    assert recorder["cmd"] == [] and recorder["reply"] == []
    assert history.known_chats_all("telegram") == []


def test_telegram_command_handler_from_stranger_is_ignored(allowlisted, recorder):
    handler = tg._make_telegram_cmd_handler("status")
    asyncio.run(handler(_tg_update(STRANGER, "/status"), _CTX))
    assert recorder == {"turn": [], "cmd": [], "reply": [], "load": []}
    assert history.known_chats_all("telegram") == []


def test_telegram_listed_user_gets_a_turn(allowlisted, recorder):
    asyncio.run(tg._telegram_handle_message(_tg_update(ALLOWED), _CTX))
    assert recorder["turn"] == [("telegram", 77, "hello")]
    assert [r[1] for r in history.known_chats_all("telegram")] == ["77"]


def test_telegram_listed_user_command_runs(allowlisted, recorder):
    handler = tg._make_telegram_cmd_handler("status")
    asyncio.run(handler(_tg_update(ALLOWED, "/status"), _CTX))
    assert recorder["cmd"] == [("telegram", "status")]


# --- Discord --------------------------------------------------------------


def test_discord_message_from_stranger_is_ignored(allowlisted, recorder, caplog):
    asyncio.run(dc._discord_handle_message(_DC_CLIENT, _dc_message(STRANGER)))
    assert recorder == {"turn": [], "cmd": [], "reply": [], "load": []}
    assert history.known_chats_all("discord") == []
    assert dc._known_discord_channels == set()
    assert f"unauthorised user id={STRANGER}" in caplog.text


def test_discord_command_from_stranger_is_ignored(allowlisted, recorder):
    asyncio.run(dc._discord_handle_message(_DC_CLIENT, _dc_message(STRANGER, "/new")))
    assert recorder["cmd"] == []
    assert history.known_chats_all("discord") == []


def test_discord_listed_user_gets_a_turn(allowlisted, recorder):
    asyncio.run(dc._discord_handle_message(_DC_CLIENT, _dc_message(ALLOWED)))
    assert recorder["turn"] == [("discord", 4242, "hello")]
    assert [r[1] for r in history.known_chats_all("discord")] == ["4242"]


def test_discord_listed_user_command_runs(allowlisted, recorder):
    asyncio.run(dc._discord_handle_message(_DC_CLIENT, _dc_message(ALLOWED, "/new")))
    assert recorder["cmd"] == [("discord", "new")]
