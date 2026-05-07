from typing import Any

from pydantic import BaseModel, Field

import memory
from registry import tool


class _SetUserPreferenceArgs(BaseModel):
    key: str = Field(description="Short identifier, e.g. 'role', 'tone', 'interests'")
    value: str = Field(
        description="Value for this preference. May contain Obsidian [[wikilinks]]."
    )


@tool(
    description=(
        "Save a structured user preference (key/value) to long-term memory. "
        "Use for stable facts about how the user wants to be addressed or what "
        "they care about, e.g. role, tone, interests, timezone."
    ),
    args=_SetUserPreferenceArgs,
)
def set_user_preference(key: str, value: str) -> dict[str, Any]:
    memory.set_preference(key, value)
    return {"status": "saved", "key": key, "value": value}


class _AddUserFactArgs(BaseModel):
    text: str = Field(description="The fact to remember. May contain Obsidian [[wikilinks]].")


@tool(
    description=(
        "Append a free-form fact about the user to long-term memory. "
        "Use for one-off facts that don't fit a key/value preference."
    ),
    args=_AddUserFactArgs,
)
def add_user_fact(text: str) -> dict[str, Any]:
    memory.add_fact(text)
    return {"status": "saved", "text": text}


class _AddUserInstructionArgs(BaseModel):
    text: str = Field(
        description=(
            "Durable behavioural rule the assistant must follow on every "
            "future turn, phrased as an imperative (e.g. 'Always reply in "
            "Finnish unless the user writes in English.', 'Use metric units "
            "for distances.'). May contain Obsidian [[wikilinks]]."
        )
    )


@tool(
    description=(
        "Save a durable instruction the assistant must follow on every "
        "subsequent turn. Use this for behavioural rules — how to respond, "
        "what to avoid, formatting preferences — not for facts about the "
        "user (use add_user_fact for those) or key/value preferences (use "
        "set_user_preference). Stored in the ## Instructions section of the "
        "memory file and re-injected into the system prompt."
    ),
    args=_AddUserInstructionArgs,
)
def add_user_instruction(text: str) -> dict[str, Any]:
    memory.add_instruction(text)
    return {"status": "saved", "text": text}


class _ForgetUserMemoryArgs(BaseModel):
    matcher: str = Field(description="Substring to match against memory entries.")


@tool(
    description=(
        "Delete a single entry from the user's long-term memory by "
        "case-insensitive substring match against the key, value, fact text, "
        "or instruction text. Forget is a final operation — if the matcher "
        "hits more than one entry the call returns status='ambiguous' with "
        "the candidate list and deletes nothing; refine the matcher and "
        "retry. Returns status='removed' with the deleted entry on a unique "
        "match, or status='not_found' if nothing matched."
    ),
    args=_ForgetUserMemoryArgs,
)
def forget_user_memory(matcher: str) -> dict[str, Any]:
    res = memory.forget(matcher)
    out: dict[str, Any] = {"status": res.status, "matcher": matcher}
    if res.entry is not None:
        out["entry"] = res.entry
    if res.matches:
        out["matches"] = res.matches
    return out


@tool(
    description=(
        "Read the full long-term memory file for this user. Returns the raw "
        "Obsidian-flavoured markdown so you can reason about preferences, facts, "
        "and instructions stored across sessions."
    ),
)
def get_user_memory() -> dict[str, Any]:
    return {"markdown": memory.render_full()}
