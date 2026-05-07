"""Live tool addition.

`/add-tool <request>` spawns Claude to write exactly one new module under
``tools/`` and runs focused gates (ruff + importable + schema-sane) before
asking the user to approve. On approve the new module is appended to
``tools/__init__.py`` so the tool survives restart; the running process
already has it because importing the module ran the ``@tool`` decorator
which mutated the global ``registry.TOOLS`` dict.
"""

from __future__ import annotations

import ast
import importlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import registry
from self_change import _dirty_tracked, _read_file, _run, _untracked

_SLUG_RE = re.compile(r"[^a-z0-9_]")


@dataclass
class LiveAddState:
    request: str
    repo_root: Path
    new_file: Path | None
    slug: str | None
    registered_names: list[str]
    gate_results: dict[str, bool]
    gate_messages: dict[str, str]
    plan_output: str
    diff: str
    aborted: str | None = None
    finalised: bool = False


@dataclass
class ApprovalResult:
    choice: str
    ok: bool
    message: str
    diff: str = ""


def run_add_tool_live(request: str, repo_root: Path) -> LiveAddState:
    pre_tracked = _dirty_tracked(repo_root)
    pre_untracked = _untracked(repo_root)
    pre_keys = set(registry.TOOLS)

    prompt = _build_prompt(request, repo_root)
    rc, stdout, stderr = _run(
        ["claude", "-p", prompt, "--allowedTools", "Edit,Write,Read"],
        cwd=repo_root,
        timeout=300,
    )
    plan_output = stdout
    if rc != 0:
        plan_output += f"\n[claude exited with code {rc}]\n{stderr}"

    new_tracked = _dirty_tracked(repo_root) - pre_tracked
    new_untracked = _untracked(repo_root) - pre_untracked
    files_changed = sorted(new_tracked | new_untracked)

    state = LiveAddState(
        request=request,
        repo_root=repo_root,
        new_file=None,
        slug=None,
        registered_names=[],
        gate_results={},
        gate_messages={},
        plan_output=plan_output,
        diff="",
    )

    abort = _validate_delta(files_changed, new_tracked)
    if abort:
        state.aborted = abort
        _revert_changes(repo_root, new_tracked, new_untracked)
        return state

    new_file_rel = files_changed[0]
    state.new_file = repo_root / new_file_rel
    state.slug = Path(new_file_rel).stem

    _, state.diff, _ = _run(["git", "diff", "--no-index", "/dev/null", new_file_rel], repo_root)

    _run_gates(state, pre_keys)
    return state


def finalise_add_tool_live(state: LiveAddState, choice: str) -> ApprovalResult:
    if state.finalised:
        return ApprovalResult(choice, False, "already finalised")
    if state.aborted:
        return ApprovalResult(choice, False, f"aborted earlier: {state.aborted}")
    if state.new_file is None or state.slug is None:
        return ApprovalResult(choice, False, "no pending tool")

    norm = choice.strip().lower().lstrip("/")

    if norm == "diff":
        return ApprovalResult(choice, True, "diff", diff=state.diff or "(no diff)")

    if norm in {"approve", "approve-force"}:
        all_passed = all(state.gate_results.values())
        if norm == "approve" and not all_passed:
            return ApprovalResult(
                choice,
                False,
                "validation failed — use /approve-force to override",
            )
        _append_to_init(state.repo_root, state.slug, state.registered_names)
        _log_change(state.repo_root, state, approved=True)
        state.finalised = True
        return ApprovalResult(
            choice,
            True,
            f"approved — {state.slug} live + persisted",
        )

    if norm == "reject":
        for name in state.registered_names:
            registry.TOOLS.pop(name, None)
        sys.modules.pop(f"tools.{state.slug}", None)
        if state.new_file.exists():
            state.new_file.unlink()
        _log_change(state.repo_root, state, approved=False)
        state.finalised = True
        return ApprovalResult(choice, True, f"rejected — {state.slug} removed")

    return ApprovalResult(choice, False, f"unknown choice: {choice}")


def _build_prompt(request: str, repo_root: Path) -> str:
    registry_src = _read_file(repo_root / "registry.py")
    init_src = _read_file(repo_root / "tools" / "__init__.py")
    sample_src = _read_file(repo_root / "tools" / "dice.py")
    return f"""You are adding ONE NEW tool to the Metalclaw bot.
Repo root: {repo_root}

User request: {request}

HARD RULES — if you violate these the change will be rejected automatically:
1. Create EXACTLY ONE new file at `tools/<slug>.py`. `<slug>` is a short
   snake_case identifier derived from the tool's purpose.
2. DO NOT edit any other file. Do not modify `tools/__init__.py`,
   `registry.py`, `bot.py`, or anything else. The harness wires the new
   tool into `tools/__init__.py` itself after you finish.
3. Decorate at least one function in the new file with `@tool` from the
   registry module (see registry source below). Use a pydantic BaseModel
   for `args=` if the tool takes arguments; pass `args=None` for
   zero-argument tools.
4. The file must be valid Python and pass `ruff check`.

=== registry.py ===
{registry_src}

=== tools/__init__.py (current — DO NOT edit) ===
{init_src}

=== tools/dice.py (small reference example) ===
{sample_src}
"""


def _validate_delta(files_changed: list[str], new_tracked: set[str]) -> str | None:
    if not files_changed:
        return "claude produced no file changes"
    if new_tracked:
        return f"existing tracked files modified: {sorted(new_tracked)}"
    if len(files_changed) > 1:
        return f"expected 1 new file, got {len(files_changed)}: {files_changed}"
    only = files_changed[0]
    p = Path(only)
    if p.parent != Path("tools") or p.suffix != ".py" or p.name == "__init__.py":
        return f"new file outside tools/<slug>.py: {only}"
    slug = p.stem
    if _SLUG_RE.search(slug) or slug.startswith("_"):
        return f"invalid slug: {slug!r} (snake_case, no leading underscore)"
    return None


def _revert_changes(repo_root: Path, new_tracked: set[str], new_untracked: set[str]) -> None:
    if new_tracked:
        _run(["git", "checkout", "--"] + sorted(new_tracked), repo_root)
    for f in sorted(new_untracked):
        (repo_root / f).unlink(missing_ok=True)


def _run_gates(state: LiveAddState, pre_keys: set[str]) -> None:
    new_file_rel = state.new_file.relative_to(state.repo_root) if state.new_file else None
    repo_root = state.repo_root

    rc, out, err = _run(["uv", "run", "ruff", "check", str(new_file_rel)], repo_root)
    state.gate_results["ruff"] = rc == 0
    state.gate_messages["ruff"] = (out + err).strip() or "ok"

    try:
        if f"tools.{state.slug}" in sys.modules:
            importlib.reload(sys.modules[f"tools.{state.slug}"])
        else:
            importlib.import_module(f"tools.{state.slug}")
        new_keys = sorted(set(registry.TOOLS) - pre_keys)
        state.registered_names = new_keys
        state.gate_results["import"] = bool(new_keys)
        state.gate_messages["import"] = (
            f"registered: {new_keys}" if new_keys else "module imported but no @tool ran"
        )
    except Exception as exc:  # noqa: BLE001
        state.gate_results["import"] = False
        state.gate_messages["import"] = f"{type(exc).__name__}: {exc}"

    schema_ok = True
    schema_msgs: list[str] = []
    for name in state.registered_names:
        t = registry.TOOLS.get(name)
        if t is None:
            schema_ok = False
            schema_msgs.append(f"{name}: missing from TOOLS")
            continue
        fn = t.schema.get("function") if isinstance(t.schema, dict) else None
        if not isinstance(fn, dict) or fn.get("name") != name or not isinstance(fn.get("parameters"), dict):
            schema_ok = False
            schema_msgs.append(f"{name}: malformed schema")
    state.gate_results["schema"] = schema_ok and bool(state.registered_names)
    state.gate_messages["schema"] = "; ".join(schema_msgs) or "ok"


def _append_to_init(repo_root: Path, slug: str, names: list[str]) -> None:
    init_path = repo_root / "tools" / "__init__.py"
    text = init_path.read_text()

    if f"from .{slug} import" in text:
        return

    import_line = f"from .{slug} import " + ", ".join(sorted(names))
    lines = text.splitlines()
    insert_at = 0
    for i, ln in enumerate(lines):
        if ln.startswith("from .") or ln.startswith("from tools."):
            insert_at = i + 1
    lines.insert(insert_at, import_line)
    text = "\n".join(lines) + ("\n" if not text.endswith("\n") else "")

    text = _merge_into_all(text, names)
    init_path.write_text(text)


def _merge_into_all(text: str, new_names: list[str]) -> str:
    tree = ast.parse(text)
    for node in tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "__all__"
            and isinstance(node.value, ast.List)
        ):
            continue
        existing = [e.value for e in node.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        merged = sorted(set(existing) | set(new_names))
        new_block = "__all__ = [\n" + "".join(f'    "{n}",\n' for n in merged) + "]"
        lines = text.splitlines()
        start = node.lineno - 1
        end = node.end_lineno or start + 1
        lines[start:end] = new_block.splitlines()
        return "\n".join(lines) + ("\n" if not text.endswith("\n") else "")
    return text


def _log_change(repo_root: Path, state: LiveAddState, *, approved: bool) -> None:
    log_path = repo_root / "changes.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "kind": "add-tool",
        "request": state.request,
        "slug": state.slug,
        "registered_names": state.registered_names,
        "gate_results": state.gate_results,
        "approved": approved,
    }
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")
