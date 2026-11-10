"""history.db connection setup."""

import sqlite3

import history


def test_history_db_uses_wal_journal_mode():
    history.turns_count("x")  # force initialisation
    other = sqlite3.connect(history._db_path())
    try:
        assert other.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        other.close()
