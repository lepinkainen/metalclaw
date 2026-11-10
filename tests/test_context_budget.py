"""Keeping replayed context bounded: tool-result elision (#49), char budget
(#55), the escalation cap (#38) and the history_trimmed note surviving a
mid-loop prompt rebuild (#50)."""

import pytest

import chat_loop
import history
import tools
from chat_loop import (
    Session,
    _active_session,
    _chat_with_provider,
    _elide_tool_results,
    _trim_to_char_budget,
    build_system_prompt,
    chat_via_escalation,
    format_now,
)
from messages import (
    AssistantMessage,
    ToolCall,
    ToolResultMessage,
    UserMessage,
    validate_exchange,
)
from registry import Tool


class _Provider:
    name = "fake"
    model = "fake"

    def __init__(self, replies):
        self._replies = iter(replies)
        self.calls: list[tuple[list, str]] = []

    def chat_once(self, history, tools, system):
        self.calls.append((list(history), system))
        return next(self._replies)


def _round(call_id: str, content: str, name: str = "read_note") -> list:
    return [
        AssistantMessage(text="", tool_calls=(ToolCall(call_id, name, {}),)),
        ToolResultMessage(call_id, name, content),
    ]


@pytest.fixture
def sys_prompt(monkeypatch):
    monkeypatch.setattr("chat_loop.build_system_prompt", lambda *_a, **_k: "sys")


# --- #49: tool results from earlier turns are elided on replay ---


def test_elide_helper_preserves_length_and_ids():
    msgs = [UserMessage("q"), *_round("c1", "x" * 50), *_round("c2", "short")]
    out, removed = _elide_tool_results(msgs, 10)
    assert len(out) == len(msgs)
    assert removed == 40
    assert out[2].call_id == "c1" and out[2].name == "read_note"
    assert (
        out[2].content.startswith("x" * 10) and "truncated 40 chars" in out[2].content
    )
    assert out[4] == msgs[4]  # under the limit: untouched
    assert out[0] is msgs[0] and out[1] is msgs[1]


def test_elide_helper_disabled():
    msgs = [UserMessage("q"), *_round("c1", "x" * 50)]
    assert _elide_tool_results(msgs, 0) == (msgs, 0)


def test_load_elides_but_db_keeps_full_result(cfg_file, write_config, sys_prompt):
    write_config(cfg_file, session_tool_result_replay_chars=2000)
    big = "y" * 10_000
    history.turns_append(
        "telegram-1", [UserMessage("read"), *_round("c1", big), AssistantMessage("ok")]
    )

    session = Session.load("telegram-1")
    result = session.history[2]
    assert isinstance(result, ToolResultMessage)
    assert result.content.startswith("y" * 2000)
    assert "truncated" in result.content
    assert len(result.content) < 2200
    assert session.history_trimmed is False
    assert history.turns_load("telegram-1")[2].content == big

    session.append_user("thanks")
    _chat_with_provider(_Provider([AssistantMessage("np")]), session)
    assert history.turns_count("telegram-1") == 6
    assert history.turns_load("telegram-1")[2].content == big


def test_load_replays_full_result_when_disabled(cfg_file, write_config, sys_prompt):
    write_config(cfg_file, session_tool_result_replay_chars=0)
    big = "y" * 10_000
    history.turns_append("telegram-2", [UserMessage("read"), *_round("c1", big)])
    assert Session.load("telegram-2").history[2].content == big


# --- #55: replay bounded by total characters ---


def _turn(text_len: int, tag: str) -> list:
    return [UserMessage(tag), AssistantMessage("a" * (text_len - len(tag)))]


def test_trim_under_budget_unchanged():
    msgs = _turn(100, "u1")
    assert _trim_to_char_budget(msgs, 1000) == (msgs, 0)


def test_trim_disabled():
    msgs = _turn(5000, "u1")
    assert _trim_to_char_budget(msgs, 0) == (msgs, 0)


def test_trim_drops_oldest_turn():
    msgs = [*_turn(1000, "u1"), *_turn(1000, "u2"), *_turn(1000, "u3")]
    kept, dropped = _trim_to_char_budget(msgs, 2500)
    assert dropped == 2
    assert kept == msgs[2:]
    assert isinstance(kept[0], UserMessage) and kept[0].content == "u2"


def test_trim_keeps_tool_round_whole():
    msgs = [
        UserMessage("u1"),
        *_round("c1", "r" * 1000),
        AssistantMessage("done"),
        UserMessage("u2"),
        *_round("c2", "s" * 500),
        AssistantMessage("done"),
    ]
    kept, dropped = _trim_to_char_budget(msgs, 800)
    assert dropped == 4
    assert kept[0] == UserMessage("u2")
    validate_exchange(kept[1], [kept[2]])


def test_trim_always_keeps_newest_turn():
    msgs = [*_turn(100, "u1"), *_turn(5000, "u2")]
    kept, dropped = _trim_to_char_budget(msgs, 1000)
    assert kept == msgs[2:] and dropped == 2


def test_trim_leading_non_user_messages_form_one_turn():
    msgs = [AssistantMessage("x" * 1000), *_turn(100, "u1")]
    kept, dropped = _trim_to_char_budget(msgs, 500)
    assert kept == msgs[1:] and dropped == 1


def test_load_char_trim_sets_flag_and_commits_delta(cfg_file, write_config):
    write_config(cfg_file, session_max_chars=2500, session_tool_result_replay_chars=0)
    for i in range(4):
        history.turns_append("discord-5", _turn(1000, f"u{i}"))

    session = Session.load("discord-5")
    assert len(session.history) == 4
    assert session.history[0] == UserMessage("u2")
    assert session.history_trimmed is True
    assert chat_loop._HISTORY_TRIMMED_NOTE in session.system

    session.append_user("next")
    _chat_with_provider(_Provider([AssistantMessage("ok")]), session)
    assert history.turns_count("discord-5") == 10


# --- #50: the trimmed note survives a mid-loop rebuild ---


def test_trimmed_note_survives_memory_mutator(monkeypatch):
    monkeypatch.setitem(
        chat_loop.TOOLS,
        "add_user_fact",
        Tool(func=lambda **_: "ok", schema={"type": "function"}),
    )
    provider = _Provider(
        [
            AssistantMessage(
                text="", tool_calls=(ToolCall("c1", "add_user_fact", {"fact": "x"}),)
            ),
            AssistantMessage("noted"),
        ]
    )
    session = Session(
        system=build_system_prompt(format_now(), history_trimmed=True),
        history_trimmed=True,
    )
    session.append_user("remember x")
    _chat_with_provider(provider, session)

    assert chat_loop._HISTORY_TRIMMED_NOTE in provider.calls[1][1]
    assert chat_loop._HISTORY_TRIMMED_NOTE in session.system


def test_snapshot_carries_history_trimmed():
    assert Session(history_trimmed=True).snapshot().history_trimmed is True


# --- #38: older tool results are capped for escalations ---


def _escalation_history() -> list:
    return [
        UserMessage("u1"),
        *_round("c1", "o" * 5000),
        AssistantMessage("a1"),
        UserMessage("u2"),
        *_round("c2", "n" * 5000),
    ]


def test_big_caps_older_results_but_keeps_latest_round(
    cfg_file, write_config, monkeypatch
):
    write_config(
        cfg_file, escalation_enabled=True, escalation_tool_result_max_chars=100
    )
    big = _Provider([AssistantMessage("answer")])
    monkeypatch.setattr("chat_loop.get_provider", lambda *a, **k: big)

    original = _escalation_history()
    session = Session(system="sys", history=list(original))
    chat_via_escalation(session)

    sent, _ = big.calls[0]
    assert sent[2].content.startswith("o" * 100) and "truncated" in sent[2].content
    assert sent[6].content == "n" * 5000  # latest round untouched
    assert session.history[: len(original)] == original  # stored history intact


def test_escalation_own_tool_rounds_pass_through(cfg_file, write_config, monkeypatch):
    write_config(cfg_file, escalation_enabled=True, escalation_tool_result_max_chars=10)
    monkeypatch.setitem(
        chat_loop.TOOLS,
        "read_note",
        Tool(func=lambda **_: "z" * 500, schema={"type": "function"}),
    )
    big = _Provider(
        [
            AssistantMessage(text="", tool_calls=(ToolCall("e1", "read_note", {}),)),
            AssistantMessage("answer"),
        ]
    )
    monkeypatch.setattr("chat_loop.get_provider", lambda *a, **k: big)
    session = Session(system="sys", history=_escalation_history())
    chat_via_escalation(session)

    second, _ = big.calls[1]
    assert "truncated" in second[2].content  # pre-escalation history capped
    assert "truncated" not in second[-1].content  # its own result is whole


def test_escalation_cap_disabled(cfg_file, write_config, monkeypatch):
    write_config(cfg_file, escalation_enabled=True, escalation_tool_result_max_chars=0)
    big = _Provider([AssistantMessage("answer")])
    monkeypatch.setattr("chat_loop.get_provider", lambda *a, **k: big)
    session = Session(system="sys", history=_escalation_history())
    chat_via_escalation(session)
    assert big.calls[0][0][2].content == "o" * 5000


def test_escalation_tool_caps_snapshot(cfg_file, write_config, monkeypatch):
    write_config(
        cfg_file, escalation_enabled=True, escalation_tool_result_max_chars=100
    )
    big = _Provider([AssistantMessage("answer")])
    monkeypatch.setattr("providers.get_provider", lambda *a, **k: big)

    snapshot = Session(system="sys", history=_escalation_history())
    token = _active_session.set(snapshot)
    try:
        out = tools.escalate_to_big_model(query="hard", reason="why")
    finally:
        _active_session.reset(token)

    assert out["status"] == "ok"
    sent, _ = big.calls[0]
    assert "truncated" in sent[2].content
    assert sent[6].content == "n" * 5000
    assert snapshot.history == _escalation_history()
