"""User manual reader. Composes a vault-stored markdown file with a live
appendix derived from ``registry.TOOLS`` and ``frontends.common.HELP_LINES``."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from config import get_config
from registry import TOOLS, tool

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "assets" / "manual_template.md"
_VAULT_FILENAME = "manual.md"

_TOOLS_REFERENCE_SLUG = "tools-reference"
_COMMANDS_REFERENCE_SLUG = "slash-command-reference"

_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_NON_SLUG_CHARS = re.compile(r"[^a-z0-9\s-]")
_WHITESPACE = re.compile(r"\s+")


def _manual_path() -> Path:
    cfg = get_config()
    return cfg.memory_dir / _VAULT_FILENAME


def _slugify(heading: str) -> str:
    s = heading.strip().lower()
    s = _NON_SLUG_CHARS.sub("", s)
    s = _WHITESPACE.sub("-", s)
    return s.strip("-")


def _load_manual() -> str | None:
    path = _manual_path()
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _split_sections(md: str) -> dict[str, str]:
    """Return slug -> section body (heading included), in document order."""
    matches = list(_H2_RE.finditer(md))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        slug = _slugify(m.group(1))
        if not slug:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        sections[slug] = md[m.start():end].rstrip() + "\n"
    return sections


def _first_sentence(body: str) -> str:
    """Pick a one-line teaser for the TOC: first non-blank prose line after the heading."""
    for raw in body.splitlines()[1:]:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("(") and line.endswith(")"):
            continue
        match = re.search(r"[.!?](?:\s|$)", line)
        if match:
            return line[: match.end()].rstrip()
        return line
    return ""


def _render_tools_appendix() -> str:
    lines = ["## Tools reference", ""]
    for name in sorted(TOOLS):
        desc = TOOLS[name].schema["function"]["description"]
        lines.append(f"### {name}")
        lines.append(desc)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_commands_appendix() -> str:
    from frontends import common  # lazy: avoids importing the frontend layer at tool-registration time

    lines = ["## Slash command reference", ""]
    for entry in common.HELP_LINES[1:]:
        lines.append(f"- {entry}")
    lines.append("")
    if common.TOOL_COMMANDS:
        lines.append("Tool commands (route directly to the named tool):")
        lines.append("")
        for slash_name, entry in common.TOOL_COMMANDS.items():
            tool_name = entry[0]
            lines.append(f"- `/{slash_name}` → `{tool_name}`")
    return "\n".join(lines).rstrip() + "\n"


def _table_of_contents(sections: dict[str, str]) -> str:
    lines = ["# Table of contents", ""]
    for slug, body in sections.items():
        teaser = _first_sentence(body)
        if teaser:
            lines.append(f"- **{slug}** — {teaser}")
        else:
            lines.append(f"- **{slug}**")
    return "\n".join(lines).rstrip() + "\n"


def _resolve_section(sections: dict[str, str], requested: str) -> str | None:
    needle = _slugify(requested)
    if needle in sections:
        return needle
    for slug in sections:
        if needle and needle in slug:
            return slug
    return None


def init_manual() -> dict[str, Any]:
    """Copy the bundled manual template into the user's vault. Refuse to overwrite."""
    dest = _manual_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return {"status": "exists", "path": str(dest)}
    shutil.copyfile(_TEMPLATE_PATH, dest)
    return {"status": "created", "path": str(dest)}


class _ReadManualArgs(BaseModel):
    section: str | None = Field(
        default=None,
        description=(
            "Section slug to fetch (e.g. 'heartbeat', 'memory-system'). "
            "Omit to get the table of contents and the list of available sections."
        ),
    )


@tool(
    description=(
        "Read Metalclaw's own user manual. Call this when the user asks "
        "'what can you do', 'how does X work', or about your own features, "
        "slash commands, memory, heartbeat, escalation, self-modification, "
        "or frontends — do not guess. With no section, returns the table of "
        "contents and the list of available section slugs; pass a slug to "
        "fetch that section's prose. The 'tools-reference' and "
        "'slash-command-reference' sections are filled in at call time from "
        "the live registry, so they reflect the current build."
    ),
    args=_ReadManualArgs,
)
def read_manual(section: str | None = None) -> dict[str, Any]:
    md = _load_manual()
    if md is None:
        return {
            "error": "manual_not_initialised",
            "hint": "Run /manual init to copy the manual template into your vault.",
        }

    sections = _split_sections(md)
    sections[_TOOLS_REFERENCE_SLUG] = _render_tools_appendix()
    sections[_COMMANDS_REFERENCE_SLUG] = _render_commands_appendix()
    available = list(sections.keys())

    if section is None or not section.strip():
        return {
            "toc": _table_of_contents(sections),
            "available_sections": available,
            "hint": "Call read_manual(section=...) to read a section.",
        }

    slug = _resolve_section(sections, section)
    if slug is None:
        return {
            "error": "unknown_section",
            "requested": section,
            "available": available,
        }
    return {"section": slug, "markdown": sections[slug]}
