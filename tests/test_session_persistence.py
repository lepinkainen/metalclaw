"""The ``turns`` table and DB-backed ``Session`` (load / commit / reset)."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import config
import history
from chat_loop import Session, _chat_with_provider, run_turn
from messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    message_from_dict,
    message_to_dict,
)


class _Provider:
    name = "fake"
    model = "fake"

    def __init__(self, replies):
        self._replies = iter(replies)

    def chat_once(self, history, tools, system):
        return next(self._replies)


ROUND = [
    UserMessage("roll"),
    AssistantMessage(text="", tool_calls=(ToolCall("c1", "roll_die", {"sides": 6}),)),
    ToolResultMessage("c1", "roll_die", '{"result": 4}'),
    AssistantMessage(text="You rolled a 4."),
]


@pytest.mark.parametrize("message", ROUND)
def test_message_dict_round_trip(message):
    assert message_from_dict(message_to_dict(message)) == message


def test_message_from_dict_rejects_unknown_kind():
    with pytest.raises(ValueError):
        message_from_dict({"kind": "system"})


def test_turns_append_load_and_reset_boundary():
    assert history.turns_load("telegram-1") == []
    history.turns_append("telegram-1", ROUND[:2])
    history.turns_append("telegram-1", ROUND[2:])
    history.turns_append("telegram-2", [UserMessage("other chat")])
    assert history.turns_load("telegram-1") == ROUND
    assert history.turns_count("telegram-1") == 4

    history.turns_reset("telegram-1")
    assert history.turns_load("telegram-1") == []
    assert history.turns_load("telegram-2") == [UserMessage("other chat")]

    history.turns_append("telegram-1", [UserMessage("after reset")])
    assert history.turns_load("telegram-1") == [UserMessage("after reset")]
    # pre-reset rows are gone from the live history but still searchable
    assert [r[3] for r in history.search_messages("rolled", 5)] == ["You rolled a 4."]


def test_session_load_commit_persists_only_new_messages(monkeypatch):
    monkeypatch.setattr("chat_loop.build_system_prompt", lambda *_a, **_k: "sys")
    history.turns_append("discord-7", [UserMessage("earlier"), AssistantMessage("yes")])

    session = Session.load("discord-7")
    assert session.history == [UserMessage("earlier"), AssistantMessage("yes")]
    assert session.scope == "discord-7"

    session.append_user("roll")
    reply = _chat_with_provider(_Provider([ROUND[1], ROUND[3]]), session)
    assert reply == "You rolled a 4."
    stored = history.turns_load("discord-7")
    # the real roll_die ran, so compare shape rather than the dice result
    assert stored[:4] == [UserMessage("earlier"), AssistantMessage("yes"), *ROUND[:2]]
    assert isinstance(stored[4], ToolResultMessage) and stored[4].call_id == "c1"
    assert stored[5] == ROUND[3]

    # A fresh load (a restart) sees the same conversation and commits only the delta.
    again = Session.load("discord-7")
    assert again.history == session.history
    again.append_user("thanks")
    _chat_with_provider(_Provider([AssistantMessage("np")]), again)
    assert history.turns_count("discord-7") == 8


def test_failed_turn_persists_nothing(monkeypatch):
    monkeypatch.setattr("chat_loop.build_system_prompt", lambda *_a, **_k: "sys")
    session = Session.load("telegram-9")

    class _Boom:
        name = model = "boom"

        def chat_once(self, *a):
            raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        asyncio.run(
            run_turn(session, "hi", lambda: _chat_with_provider(_Boom(), session))
        )
    assert session.history == []
    assert history.turns_load("telegram-9") == []


def test_scopeless_session_stays_in_memory():
    session = Session(system="sys")
    session.append_user("hi")
    _chat_with_provider(_Provider([AssistantMessage("hello")]), session)
    assert session.history[-1] == AssistantMessage("hello")
    assert history.search_messages("hello", 5) == []


def test_session_reset_writes_boundary():
    history.turns_append("telegram-3", [UserMessage("old")])
    Session.reset("telegram-3")
    assert history.turns_load("telegram-3") == []


# --- trimming: idle expiry + message window ---


def _age_rows(session: str, seconds: int) -> None:
    """Backdate every row of ``session`` so it looks ``seconds`` old."""
    old = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    with history._LOCK:
        db = history._conn()
        db.execute("UPDATE turns SET ts = ? WHERE session = ?", (old, session))
        db.commit()


def test_turns_window_cuts_at_a_user_message():
    history.turns_append(
        "telegram-4", [UserMessage("first"), AssistantMessage("a1"), *ROUND]
    )
    msgs, skipped = history.turns_window("telegram-4", 3)
    # the last 3 rows would start inside the tool round; the cut moves back to "roll"
    assert msgs == ROUND and skipped == 2
    assert history.turns_window("telegram-4", 0) == (
        [UserMessage("first"), AssistantMessage("a1"), *ROUND],
        0,
    )
    assert history.turns_window("telegram-4", 100)[1] == 0


def test_turns_window_walks_back_to_the_opening_user_row():
    history.turns_append(
        "telegram-5", [UserMessage("x"), AssistantMessage("a"), AssistantMessage("b")]
    )
    msgs, skipped = history.turns_window("telegram-5", 1)
    assert skipped == 0 and len(msgs) == 3


def test_session_load_idle_reset_and_note(cfg_file, write_config, monkeypatch):
    write_config(cfg_file, session_idle_reset_seconds=60)
    config.reset_cache()
    monkeypatch.setattr("chat_loop.memory.summary", lambda: "")
    history.turns_append(
        "discord-1", [UserMessage("yesterday"), AssistantMessage("ok")]
    )

    fresh = Session.load("discord-1")
    assert fresh.history == [UserMessage("yesterday"), AssistantMessage("ok")]
    assert fresh.history_trimmed is False
    assert "archived" not in fresh.system

    _age_rows("discord-1", 120)
    expired = Session.load("discord-1")
    assert expired.history == [] and expired.history_trimmed is True
    assert "search_conversation_history" in expired.system
    assert history.turns_last_ts("discord-1") is None  # boundary written
    assert history.search_messages("yesterday", 5)  # still searchable

    # the turn after the reset is a normal, un-noted conversation again
    expired.append_user("hi")
    _chat_with_provider(_Provider([AssistantMessage("hello")]), expired)
    assert Session.load("discord-1").history_trimmed is False


def test_session_load_window_sets_note(cfg_file, write_config, monkeypatch):
    write_config(cfg_file, session_max_messages=4, session_idle_reset_seconds=0)
    config.reset_cache()
    monkeypatch.setattr("chat_loop.memory.summary", lambda: "")
    history.turns_append(
        "discord-2", [UserMessage("old"), AssistantMessage("o"), *ROUND]
    )
    _age_rows("discord-2", 10_000)  # idle rule disabled, so age must not matter

    s = Session.load("discord-2")
    assert s.history == ROUND and s.history_trimmed is True
    assert "archived" in s.system
    # commit after a windowed load appends after the full row set, not the window
    s.append_user("more")
    _chat_with_provider(_Provider([AssistantMessage("sure")]), s)
    assert history.turns_count("discord-2") == 8
