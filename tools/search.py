from typing import Any

from pydantic import BaseModel, Field

import vault_search
from registry import tool


class _SearchVaultArgs(BaseModel):
    query: str = Field(description="Ripgrep regex or literal text to search for.")
    max_results: int = Field(
        default=20, description="Maximum number of hits to return (default 20, max 200)."
    )
    context_lines: int = Field(
        default=1,
        description="Lines of context before and after each match (default 1, max 10).",
    )


@tool(
    description=(
        "Search the user's Obsidian vault for notes matching a query (ripgrep "
        "regex or literal text). Returns snippets with file paths and line "
        "numbers. Use read_note afterwards to fetch the full body of a "
        "promising hit."
    ),
    args=_SearchVaultArgs,
)
def search_vault(
    query: str,
    max_results: int = 20,
    context_lines: int = 1,
) -> dict[str, Any]:
    return vault_search.search(query, max_results=max_results, context_lines=context_lines)


class _ReadNoteArgs(BaseModel):
    path: str = Field(
        description="Path relative to the vault root, e.g. 'Projects/Metalclaw.md'."
    )


@tool(
    description=(
        "Read a markdown note from the user's Obsidian vault by path relative "
        "to the vault root (e.g. 'Projects/Metalclaw.md'). Refuses paths "
        "outside the vault and non-markdown files."
    ),
    args=_ReadNoteArgs,
)
def read_note(path: str) -> dict[str, Any]:
    return vault_search.read(path)
