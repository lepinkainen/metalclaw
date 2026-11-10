"""history.db connection setup and the ``turns_fts`` search index."""

import json
import sqlite3

import history
from messages import AssistantMessage, ToolCall, ToolResultMessage, UserMessage


def test_history_db_uses_wal_journal_mode():
    history.turns_count("x")  # force initialisation
    other = sqlite3.connect(history._db_path())
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        other.close()


# --- search_messages: turns_fts trigram index ---


def _search(query: str, limit: int = 10) -> list[str]:
    return [r[3] for r in history.search_messages(query, limit)]


def test_search_matches_substring_inside_a_word_case_insensitively():
    history.turns_append(
        "telegram-1", [UserMessage("roll a die"), AssistantMessage("You rolled a 4.")]
    )
    assert _search("olle") == ["You rolled a 4."]
    assert _search("ROLLED") == ["You rolled a 4."]


def test_search_quotes_and_operators_are_literal():
    text = 'he said "hi" to the e-mail bot: ok*'
    history.turns_append("telegram-1", [UserMessage(text)])
    assert _search('"hi"') == [text]
    assert _search("e-mail") == [text]
    assert _search("bot: ok*") == [text]
    assert _search('a"b') == []


def test_search_short_query_uses_like_fallback():
    history.turns_append("telegram-1", [UserMessage("go ok")])
    assert _search("ok") == ["go ok"]
    assert _search("OK") == ["go ok"]


def test_search_without_fts_falls_back_to_like(monkeypatch):
    history.turns_append("telegram-1", [UserMessage("You rolled a 4.")])
    monkeypatch.setattr(history, "_fts_available", False)
    assert _search("olle") == ["You rolled a 4."]


def test_search_skips_tool_calls_and_results():
    history.turns_append(
        "discord-9",
        [
            UserMessage("roll"),
            AssistantMessage(text="", tool_calls=[ToolCall("c1", "roll_die", {})]),
            ToolResultMessage("c1", "roll_die", '{"secretvalue": 4}'),
            AssistantMessage(text="done"),
        ],
    )
    assert _search("secretvalue") == []
    assert _search("roll_die") == []


def test_search_newest_first_and_limit(monkeypatch):
    for i, day in enumerate(("2026-01-01", "2026-01-03", "2026-01-02")):
        monkeypatch.setattr(history, "_now", lambda d=day: f"{d}T00:00:00+00:00")
        history.turns_append("telegram-1", [UserMessage(f"needle {i}")])
    assert _search("needle") == ["needle 1", "needle 2", "needle 0"]
    assert _search("needle", limit=2) == ["needle 1", "needle 2"]


def _raw_insert(sql: str, params: tuple) -> None:
    raw = sqlite3.connect(history._db_path())
    try:
        raw.execute(sql, params)
        raw.commit()
    finally:
        raw.close()


def test_fts_backfill_indexes_existing_turns_and_legacy_rows(monkeypatch):
    history.turns_count("x")  # create the schema; turns_fts starts empty
    turn_sql = "INSERT INTO turns (session, ts, kind, payload) VALUES (?, ?, ?, ?)"
    _raw_insert(
        turn_sql,
        (
            "telegram-1",
            "2026-01-02T00:00:00+00:00",
            "assistant",
            json.dumps({"kind": "assistant", "text": "backfilled turn"}),
        ),
    )
    _raw_insert(
        turn_sql,
        (
            "telegram-1",
            "2026-01-02T00:00:01+00:00",
            "tool_result",
            json.dumps({"kind": "tool_result", "content": "backfilled tool"}),
        ),
    )
    _raw_insert(
        "INSERT INTO messages (session, ts, role, content) VALUES (?, ?, ?, ?)",
        ("20250101T000000", "2025-01-01T00:00:00+00:00", "user", "backfilled cli"),
    )
    assert history.search_messages("backfilled", 10) == []  # not indexed yet

    monkeypatch.setattr(history, "_shared", None)  # re-run _init on the same path
    rows = history.search_messages("backfilled", 10)
    assert [(r[1], r[2], r[3]) for r in rows] == [
        ("telegram-1", "assistant", "backfilled turn"),
        ("20250101T000000", "user", "backfilled cli"),
    ]
    # the backfill runs once: re-opening a non-empty index adds nothing
    monkeypatch.setattr(history, "_shared", None)
    assert len(history.search_messages("backfilled", 10)) == 2
