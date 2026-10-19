import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

from prompt_toolkit.history import History

from config import xdg_data_dir


def _db_path() -> Path:
    return xdg_data_dir() / "history.db"


def _init(path: Path) -> sqlite3.Connection:
    # check_same_thread=False: tools run in executor threads; all access goes
    # through _LOCK below.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            session TEXT    NOT NULL,
            ts      TEXT    NOT NULL,
            role    TEXT    NOT NULL,
            content TEXT    NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ts                TEXT    NOT NULL,
            provider          TEXT    NOT NULL,
            model             TEXT    NOT NULL,
            prompt_tokens     INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            cost_usd          REAL
        )
    """)
    conn.commit()
    return conn


# One shared connection per db path (tests repoint XDG_DATA_HOME), serialised by a lock.
_LOCK = threading.Lock()
_shared: tuple[Path, sqlite3.Connection] | None = None


def _conn() -> sqlite3.Connection:
    """Caller must hold _LOCK."""
    global _shared
    path = _db_path()
    if _shared is None or _shared[0] != path:
        _shared = (path, _init(path))
    return _shared[1]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def record_message(session: str, role: str, content: str) -> None:
    with _LOCK:
        db = _conn()
        db.execute(
            "INSERT INTO messages (session, ts, role, content) VALUES (?, ?, ?, ?)",
            (session, _now(), role, content),
        )
        db.commit()


def search_messages(query: str, limit: int) -> list[tuple[str, str, str, str]]:
    """Case-insensitive substring match, newest first: (ts, session, role, content)."""
    like = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _LOCK:
        return (
            _conn()
            .execute(
                "SELECT ts, session, role, content FROM messages "
                "WHERE content LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (f"%{like}%", limit),
            )
            .fetchall()
        )


def record_usage(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None,
) -> None:
    with _LOCK:
        db = _conn()
        db.execute(
            "INSERT INTO usage (ts, provider, model, prompt_tokens, completion_tokens, cost_usd)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), provider, model, prompt_tokens, completion_tokens, cost_usd),
        )
        db.commit()


def usage_since(
    since_iso: str,
) -> list[tuple[str, str, int, int, int, float | None, int]]:
    """Per (provider, model): calls, prompt, completion, SUM(cost) (None if all unknown), calls with known cost."""
    with _LOCK:
        return (
            _conn()
            .execute(
                "SELECT provider, model, COUNT(*), SUM(prompt_tokens), SUM(completion_tokens),"
                " SUM(cost_usd), COUNT(cost_usd) FROM usage WHERE ts >= ?"
                " GROUP BY provider, model ORDER BY provider, model",
                (since_iso,),
            )
            .fetchall()
        )


class SQLiteHistory(History):
    """prompt_toolkit History backed by SQLite.

    User inputs are saved via store_string() (called automatically by
    prompt_toolkit on submit), giving cross-session up-arrow history.
    Assistant replies must be saved explicitly via save_assistant().
    """

    def __init__(self, session: str) -> None:
        self._session = session
        super().__init__()

    def load_history_strings(self):
        # CLI session ids are bare timestamps; Telegram/Discord rows are
        # "<surface>-<id>" and must not leak into up-arrow history.
        with _LOCK:
            rows = (
                _conn()
                .execute(
                    "SELECT content FROM messages WHERE role = 'user' "
                    "AND session NOT LIKE '%-%' ORDER BY id DESC"
                )
                .fetchall()
            )
        return [row[0] for row in rows]

    def store_string(self, string: str) -> None:
        record_message(self._session, "user", string)

    def save_assistant(self, content: str) -> None:
        record_message(self._session, "assistant", content)
