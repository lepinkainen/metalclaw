import json
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heartbeat_state (
            action_id     TEXT    PRIMARY KEY,
            last_run      TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            last_failed   TEXT,
            updated_at    TEXT    NOT NULL,
            meta          TEXT    NOT NULL DEFAULT '{}'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS known_chats (
            frontend   TEXT NOT NULL,
            chat_id    TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen  TEXT NOT NULL,
            meta       TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (frontend, chat_id)
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


# --- heartbeat_state: per-action scheduler state (owned by heartbeat.py) ---
#
# One row per action id. ``last_run`` drives the due logic, ``failure_count`` /
# ``last_failed`` drive retry backoff, ``meta`` is a free-form JSON object for
# after-the-fact debugging (kind, channel, prompt snippet, last outcome, last
# error, …) that heartbeat.py merges on every write. This module stays
# heartbeat-agnostic: rows in, rows out, no imports of heartbeat.

HeartbeatStateRow = tuple[str, str | None, int, str | None, str, str]
"""(action_id, last_run, failure_count, last_failed, updated_at, meta_json)"""

_HB_COLS = "action_id, last_run, failure_count, last_failed, updated_at, meta"


def _merge_meta(db: sqlite3.Connection, action_id: str, patch: dict) -> str:
    row = db.execute(
        "SELECT meta FROM heartbeat_state WHERE action_id = ?", (action_id,)
    ).fetchone()
    return _merge_json(row[0] if row else None, patch)


def heartbeat_state_all() -> list[HeartbeatStateRow]:
    with _LOCK:
        return (
            _conn()
            .execute(f"SELECT {_HB_COLS} FROM heartbeat_state ORDER BY action_id")
            .fetchall()
        )


def heartbeat_state_get(action_id: str) -> HeartbeatStateRow | None:
    with _LOCK:
        return (
            _conn()
            .execute(
                f"SELECT {_HB_COLS} FROM heartbeat_state WHERE action_id = ?",
                (action_id,),
            )
            .fetchone()
        )


def heartbeat_mark_success(action_id: str, ran_at: str, meta: dict) -> None:
    """Stamp ``last_run`` and clear the failure counter."""
    with _LOCK:
        db = _conn()
        merged = _merge_meta(db, action_id, meta)
        db.execute(
            "INSERT INTO heartbeat_state"
            " (action_id, last_run, failure_count, last_failed, updated_at, meta)"
            " VALUES (?, ?, 0, NULL, ?, ?)"
            " ON CONFLICT(action_id) DO UPDATE SET"
            "  last_run = excluded.last_run, failure_count = 0, last_failed = NULL,"
            "  updated_at = excluded.updated_at, meta = excluded.meta",
            (action_id, ran_at, _now(), merged),
        )
        db.commit()


def heartbeat_mark_failure(action_id: str, failed_at: str, meta: dict) -> int:
    """Increment the failure counter; ``last_run`` is untouched. Returns the new count."""
    with _LOCK:
        db = _conn()
        merged = _merge_meta(db, action_id, meta)
        row = db.execute(
            "INSERT INTO heartbeat_state"
            " (action_id, last_run, failure_count, last_failed, updated_at, meta)"
            " VALUES (?, NULL, 1, ?, ?, ?)"
            " ON CONFLICT(action_id) DO UPDATE SET"
            "  failure_count = failure_count + 1, last_failed = excluded.last_failed,"
            "  updated_at = excluded.updated_at, meta = excluded.meta"
            " RETURNING failure_count",
            (action_id, failed_at, _now(), merged),
        ).fetchone()
        db.commit()
        return int(row[0])


def heartbeat_state_forget(action_id: str) -> bool:
    with _LOCK:
        db = _conn()
        cur = db.execute(
            "DELETE FROM heartbeat_state WHERE action_id = ?", (action_id,)
        )
        db.commit()
        return cur.rowcount > 0


def heartbeat_state_prune(keep: set[str]) -> int:
    """Delete rows for ids not in ``keep``. Returns the number removed."""
    with _LOCK:
        db = _conn()
        if keep:
            placeholders = ",".join("?" * len(keep))
            cur = db.execute(
                f"DELETE FROM heartbeat_state WHERE action_id NOT IN ({placeholders})",
                tuple(sorted(keep)),
            )
        else:
            cur = db.execute("DELETE FROM heartbeat_state")
        db.commit()
        return cur.rowcount


def heartbeat_state_import(rows: list[HeartbeatStateRow]) -> int:
    """Bulk upsert (legacy JSON migration). Returns rows written."""
    with _LOCK:
        db = _conn()
        db.executemany(
            "INSERT INTO heartbeat_state"
            " (action_id, last_run, failure_count, last_failed, updated_at, meta)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(action_id) DO UPDATE SET"
            "  last_run = COALESCE(excluded.last_run, last_run),"
            "  failure_count = excluded.failure_count, last_failed = excluded.last_failed,"
            "  updated_at = excluded.updated_at, meta = excluded.meta",
            rows,
        )
        db.commit()
        return len(rows)


# --- known_chats: chats/channels each frontend has seen (heartbeat fan-out) ---
#
# One row per (frontend, chat_id). ``Channel.active_scopes()`` reads it so
# heartbeat alerts can be fanned out after a restart; ``meta`` is a JSON
# debugging aid (username, chat title, guild, …) merged on every touch.

KnownChatRow = tuple[str, str, str, str, str]
"""(frontend, chat_id, first_seen, last_seen, meta_json)"""


def _merge_known_chat_meta(
    db: sqlite3.Connection, frontend: str, chat_id: str, patch: dict
) -> str:
    row = db.execute(
        "SELECT meta FROM known_chats WHERE frontend = ? AND chat_id = ?",
        (frontend, chat_id),
    ).fetchone()
    return _merge_json(row[0] if row else None, patch)


def _merge_json(current_json: str | None, patch: dict) -> str:
    current: dict = {}
    if current_json:
        try:
            loaded = json.loads(current_json)
            if isinstance(loaded, dict):
                current = loaded
        except json.JSONDecodeError:
            current = {"_corrupt_meta": current_json}
    current.update({k: v for k, v in patch.items() if v is not None})
    return json.dumps(current, ensure_ascii=False, sort_keys=True)


def known_chat_touch(frontend: str, chat_id: str, meta: dict | None = None) -> bool:
    """Upsert a chat: insert sets ``first_seen``, every call bumps ``last_seen``
    and merges ``meta``. Returns True when the row was newly created."""
    now = _now()
    with _LOCK:
        db = _conn()
        merged = _merge_known_chat_meta(db, frontend, chat_id, meta or {})
        cur = db.execute(
            "INSERT INTO known_chats (frontend, chat_id, first_seen, last_seen, meta)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(frontend, chat_id) DO UPDATE SET"
            "  last_seen = excluded.last_seen, meta = excluded.meta"
            " RETURNING first_seen",
            (frontend, chat_id, now, now, merged),
        )
        row = cur.fetchone()
        db.commit()
        return bool(row) and row[0] == now


def known_chat_ids(frontend: str) -> list[str]:
    with _LOCK:
        rows = (
            _conn()
            .execute(
                "SELECT chat_id FROM known_chats WHERE frontend = ? ORDER BY chat_id",
                (frontend,),
            )
            .fetchall()
        )
    return [r[0] for r in rows]


def known_chats_all(frontend: str | None = None) -> list[KnownChatRow]:
    with _LOCK:
        db = _conn()
        if frontend is None:
            return db.execute(
                "SELECT frontend, chat_id, first_seen, last_seen, meta FROM known_chats"
                " ORDER BY frontend, chat_id"
            ).fetchall()
        return db.execute(
            "SELECT frontend, chat_id, first_seen, last_seen, meta FROM known_chats"
            " WHERE frontend = ? ORDER BY chat_id",
            (frontend,),
        ).fetchall()


def known_chat_forget(frontend: str, chat_id: str) -> bool:
    with _LOCK:
        db = _conn()
        cur = db.execute(
            "DELETE FROM known_chats WHERE frontend = ? AND chat_id = ?",
            (frontend, chat_id),
        )
        db.commit()
        return cur.rowcount > 0


def known_chats_import(frontend: str, chat_ids: list[str], meta: dict) -> int:
    """Bulk insert (legacy JSON migration); existing rows are left untouched."""
    now = _now()
    meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True)
    with _LOCK:
        db = _conn()
        db.executemany(
            "INSERT OR IGNORE INTO known_chats (frontend, chat_id, first_seen, last_seen, meta)"
            " VALUES (?, ?, ?, ?, ?)",
            [(frontend, cid, now, now, meta_json) for cid in chat_ids],
        )
        db.commit()
        return len(chat_ids)
