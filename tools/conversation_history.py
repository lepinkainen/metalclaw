import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

import history
from registry import tool

log = logging.getLogger(__name__)

_CONTENT_LIMIT = 500


class _SearchConversationHistoryArgs(BaseModel):
    query: str = Field(description="Text to look for (case-insensitive substring).")
    limit: int = Field(
        default=10,
        description="Maximum number of messages to return (default 10, max 50).",
    )


@tool(
    description=(
        "Search past conversations (Telegram, Discord; user messages and "
        "assistant replies) for a substring. Returns matches newest first with "
        "local timestamp, surface, role and trimmed content. Use when the user asks "
        "what they or you said earlier, or refers to a past conversation."
    ),
    args=_SearchConversationHistoryArgs,
)
def search_conversation_history(query: str, limit: int = 10) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    log.info("search_conversation_history query=%r limit=%d", query, limit)
    rows = history.search_messages(query, limit)
    log.info("search_conversation_history matches=%d", len(rows))
    return {
        "matches": [
            {
                "ts": datetime.fromisoformat(ts)
                .astimezone()
                .strftime("%Y-%m-%d %H:%M"),
                # Legacy CLI rows are bare timestamps; others are "<surface>-<id>".
                "surface": session if "-" in session else "cli",
                "role": role,
                "content": content[:_CONTENT_LIMIT]
                + ("…" if len(content) > _CONTENT_LIMIT else ""),
            }
            for ts, session, role, content in rows
        ]
    }
