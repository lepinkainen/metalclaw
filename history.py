import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from prompt_toolkit.history import History


def _db_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    p = Path(xdg) / "metalclaw" / "history.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _init(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT    NOT NULL,
            ts      TEXT    NOT NULL,
            role    TEXT    NOT NULL,
            content TEXT    NOT NULL
        )
    """)
    conn.commit()
    return conn


class SQLiteHistory(History):
    """prompt_toolkit History backed by SQLite.

    User inputs are saved via store_string() (called automatically by
    prompt_toolkit on submit), giving cross-session up-arrow history.
    Assistant replies must be saved explicitly via save_assistant().
    """

    def __init__(self, session: str) -> None:
        self._session = session
        self._db = _init(_db_path())
        super().__init__()

    def load_history_strings(self):
        rows = self._db.execute(
            "SELECT content FROM messages WHERE role = 'user' ORDER BY id DESC"
        ).fetchall()
        return [row[0] for row in rows]

    def store_string(self, string: str) -> None:
        self._db.execute(
            "INSERT INTO messages (session, ts, role, content) VALUES (?, ?, ?, ?)",
            (self._session, _now(), "user", string),
        )
        self._db.commit()

    def save_assistant(self, content: str) -> None:
        self._db.execute(
            "INSERT INTO messages (session, ts, role, content) VALUES (?, ?, ?, ?)",
            (self._session, _now(), "assistant", content),
        )
        self._db.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
