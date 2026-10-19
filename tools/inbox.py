import fcntl
import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from config import get_config
from registry import tool

log = logging.getLogger(__name__)

# Fixed on purpose: the tool takes no path, so it can never write elsewhere.
_INBOX_FILENAME = "INBOX.md"


class _AppendToInboxArgs(BaseModel):
    text: str = Field(
        description="What to note down: a thought, task, or link, as plain text or markdown."
    )


@tool(
    description=(
        "Append a timestamped entry to INBOX.md in the root of the user's Obsidian "
        "vault. Use for 'note this down', 'add to my inbox', 'save this link', "
        "'jot this down' requests. This is the only file it can write; it is not "
        "for reminders (use create_heartbeat_action) or preferences (use memory)."
    ),
    args=_AppendToInboxArgs,
)
def append_to_inbox(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("text must not be empty")
    root = get_config().vault_path
    if not root.is_dir():
        raise RuntimeError(f"vault_path {root} is not a directory")
    path = root / _INBOX_FILENAME
    # Continuation lines are indented so a multi-line note stays one list item.
    body = "\n  ".join(text.splitlines())
    entry = f"- {datetime.now().strftime('%Y-%m-%d %H:%M')} — {body}\n"
    log.info("append_to_inbox path=%s text=%.200s", path, text)
    # flock on the append fd serialises threads and other processes alike.
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(entry)
            f.flush()
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return {"status": "saved", "file": _INBOX_FILENAME, "entry": entry.strip()}
