"""Search and read the user's Obsidian vault via ripgrep.

`search` shells out to `rg --json --type md` against `vault_path` from config,
applying any user-configured exclude globs. `read` returns the body of a single
markdown note by path relative to the vault root, with a path-traversal guard.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from config import get_config


_LINE_CHAR_LIMIT = 300
_BODY_CHAR_LIMIT = 50_000


def _vault_root() -> Path:
    return get_config().vault_path.expanduser().resolve()


def _decode_text(payload: dict[str, Any]) -> str:
    """rg --json wraps text in {'text': str} or {'bytes': base64}. We only
    care about UTF-8 lines; binary matches collapse to the empty string."""
    if "text" in payload:
        return payload["text"]
    return ""


def _trim(line: str) -> str:
    line = line.rstrip("\n")
    if len(line) > _LINE_CHAR_LIMIT:
        line = line[:_LINE_CHAR_LIMIT] + "…"
    return line


def search(
    query: str,
    max_results: int = 20,
    context_lines: int = 1,
) -> dict[str, Any]:
    """Run ripgrep over the vault, return structured hits.

    Returns:
        {
            "query": str,
            "vault": str (absolute vault path),
            "hits": [
                {
                    "path": str (relative to vault),
                    "line_number": int,
                    "line": str (matched line, trimmed),
                    "before": [str, ...] (preceding context),
                    "after":  [str, ...] (following context),
                },
                ...
            ],
            "truncated": bool (true if cut off at max_results),
        }
    """
    if not query:
        raise ValueError("query is required")
    rg = shutil.which("rg")
    if rg is None:
        raise RuntimeError("ripgrep (rg) not found on PATH")

    max_results = max(1, min(int(max_results), 200))
    context_lines = max(0, min(int(context_lines), 10))

    cfg = get_config()
    vault = _vault_root()

    cmd: list[str] = [
        rg,
        "--json",
        "--type", "md",
        "--smart-case",
        "--context", str(context_lines),
        "--max-count", str(max_results),
    ]
    for pat in cfg.vault_search_excludes:
        cmd.extend(["--glob", f"!{pat}"])
    # Run with cwd at the vault root so user-supplied glob patterns
    # (e.g. "Notion Export/**") anchor relative to the vault, not cwd.
    cmd.extend(["--", query, "."])

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        cwd=str(vault),
    )
    # rg exits 0 on matches, 1 on no matches, 2 on real error.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"rg failed (exit {proc.returncode}): {proc.stderr.strip()}")

    hits: list[dict[str, Any]] = []
    pending_before: dict[str, list[str]] = {}
    last_match_idx: dict[str, int] = {}
    current_path: str | None = None
    truncated = False

    for raw_line in proc.stdout.splitlines():
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        kind = event.get("type")
        data = event.get("data", {})

        if kind == "begin":
            current_path = _decode_text(data.get("path", {}))
            pending_before.setdefault(current_path, [])
            continue

        if kind == "end":
            current_path = None
            continue

        if current_path is None:
            continue

        text = _decode_text(data.get("lines", {}))
        line_no = data.get("line_number")

        if kind == "context":
            trimmed = _trim(text)
            if last_match_idx.get(current_path) is not None:
                idx = last_match_idx[current_path]
                hits[idx]["after"].append(trimmed)
            else:
                pending_before.setdefault(current_path, []).append(trimmed)
            continue

        if kind == "match":
            if len(hits) >= max_results:
                truncated = True
                break
            p = Path(current_path)
            if p.is_absolute():
                try:
                    rel = str(p.relative_to(vault))
                except ValueError:
                    rel = current_path
            else:
                rel = current_path[2:] if current_path.startswith("./") else current_path
            before = pending_before.pop(current_path, [])
            hits.append(
                {
                    "path": rel,
                    "line_number": line_no,
                    "line": _trim(text),
                    "before": before,
                    "after": [],
                }
            )
            last_match_idx[current_path] = len(hits) - 1
            pending_before.setdefault(current_path, [])

    return {
        "query": query,
        "vault": str(vault),
        "hits": hits,
        "truncated": truncated,
    }


def read(path: str) -> dict[str, Any]:
    """Read a markdown note inside the vault.

    `path` is relative to the vault root. Path traversal outside the vault is
    refused. Only `.md` files are accepted. Bodies over `_BODY_CHAR_LIMIT` are
    truncated and `truncated: True` is set.
    """
    if not path:
        raise ValueError("path is required")
    vault = _vault_root()
    candidate = (vault / path).resolve()
    if not candidate.is_relative_to(vault):
        raise ValueError(f"path '{path}' resolves outside the vault")
    if candidate.suffix.lower() != ".md":
        raise ValueError(f"only markdown notes can be read (got '{candidate.suffix}')")
    if not candidate.is_file():
        raise FileNotFoundError(f"note not found: {path}")

    body = candidate.read_text(encoding="utf-8")
    truncated = False
    if len(body) > _BODY_CHAR_LIMIT:
        body = body[:_BODY_CHAR_LIMIT]
        truncated = True

    rel = str(candidate.relative_to(vault))
    return {
        "path": rel,
        "body": body,
        "truncated": truncated,
        "size_bytes": candidate.stat().st_size,
    }
