"""Tests for live tool registration.

Patch subprocess.run (the deepest layer) to control git/claude/ruff
output. ``importlib.import_module`` is patched per-test so the
fixture file doesn't have to live on sys.path; the patch mutates
``registry.TOOLS`` in place to simulate the real ``@tool``
decorator side-effect.
"""

import json
import types
from unittest.mock import MagicMock, patch

import pytest

import live_tool
import registry
import self_change


def _completed(rc=0, stdout="", stderr=""):
    r = MagicMock()
    r.returncode = rc
    r.stdout = stdout
    r.stderr = stderr
    return r


@pytest.fixture
def fake_repo(tmp_path):
    """Minimal repo with a tools/ package and an __init__.py shaped like the real one."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "__init__.py").write_text(
        '"""docstring."""\n\nfrom .existing import existing_tool\n\n__all__ = [\n    "existing_tool",\n]\n'
    )
    return tmp_path


@pytest.fixture
def clean_tools_dict():
    snapshot = dict(registry.TOOLS)
    yield
    registry.TOOLS.clear()
    registry.TOOLS.update(snapshot)


def _fake_import_registers(slug: str, names: list[str]):
    def fake(modname):
        assert modname == f"tools.{slug}"
        for name in names:
            registry.TOOLS[name] = registry.Tool(
                func=lambda: None,
                schema={
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "fake",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                },
                args_model=None,
            )
        return types.SimpleNamespace(__name__=modname)

    return fake


def _patch_run(side_effects):
    """Patch subprocess.run with a sequenced side-effect that calls
    callables (so a test can perform filesystem side-effects when
    "claude" is invoked) instead of returning them as values.
    """
    it = iter(side_effects)

    def _side(*args, **kwargs):
        item = next(it)
        if callable(item) and not hasattr(item, "returncode"):
            return item(*args, **kwargs)
        return item

    return patch("subprocess.run", side_effect=_side)


# --- Happy path ---


def test_happy_path_registers_and_finalise_persists(fake_repo, clean_tools_dict):
    new_file = fake_repo / "tools" / "ping.py"

    def claude_writes(*args, **kwargs):
        new_file.write_text("# fake tool\n")
        return _completed(0, "ok")

    side_effects = [
        _completed(0, ""),                 # pre tracked
        _completed(0, ""),                 # pre untracked
        claude_writes,                     # claude -p
        _completed(0, ""),                 # post tracked
        _completed(0, "tools/ping.py\n"),  # post untracked
        _completed(0, "diff body"),        # git diff --no-index
        _completed(0, ""),                 # ruff
    ]

    with _patch_run(side_effects), patch.object(
        live_tool.importlib, "import_module", side_effect=_fake_import_registers("ping", ["ping_tool"])
    ):
        state = live_tool.run_add_tool_live("add ping", fake_repo)

    assert state.aborted is None
    assert state.slug == "ping"
    assert state.registered_names == ["ping_tool"]
    assert state.gate_results == {"ruff": True, "import": True, "schema": True}

    res = live_tool.finalise_add_tool_live(state, "approve")
    assert res.ok
    init = (fake_repo / "tools" / "__init__.py").read_text()
    assert "from .ping import ping_tool" in init
    assert '"ping_tool"' in init
    log = (fake_repo / "changes.jsonl").read_text().strip()
    assert json.loads(log)["approved"] is True


# --- Reject path ---


def test_reject_unlinks_file_and_pops_registry(fake_repo, clean_tools_dict):
    new_file = fake_repo / "tools" / "ping.py"

    def claude_writes(*args, **kwargs):
        new_file.write_text("# fake\n")
        return _completed(0, "ok")

    side_effects = [
        _completed(0, ""),
        _completed(0, ""),
        claude_writes,
        _completed(0, ""),
        _completed(0, "tools/ping.py\n"),
        _completed(0, "diff"),
        _completed(0, ""),
    ]

    with _patch_run(side_effects), patch.object(
        live_tool.importlib, "import_module", side_effect=_fake_import_registers("ping", ["ping_tool"])
    ):
        state = live_tool.run_add_tool_live("add ping", fake_repo)

    assert "ping_tool" in registry.TOOLS

    res = live_tool.finalise_add_tool_live(state, "reject")
    assert res.ok
    assert not new_file.exists()
    assert "ping_tool" not in registry.TOOLS


# --- Violations ---


def test_aborts_when_multiple_new_files(fake_repo, clean_tools_dict):
    (fake_repo / "tools" / "a.py").write_text("# a\n")
    (fake_repo / "tools" / "b.py").write_text("# b\n")

    side_effects = [
        _completed(0, ""),
        _completed(0, ""),
        _completed(0, "ok"),
        _completed(0, ""),
        _completed(0, "tools/a.py\ntools/b.py\n"),
    ]

    with _patch_run(side_effects):
        state = live_tool.run_add_tool_live("add two", fake_repo)

    assert state.aborted is not None
    assert "expected 1 new file" in state.aborted
    assert not (fake_repo / "tools" / "a.py").exists()
    assert not (fake_repo / "tools" / "b.py").exists()


def test_aborts_when_file_outside_tools(fake_repo, clean_tools_dict):
    (fake_repo / "bot.py").write_text("# rogue\n")

    side_effects = [
        _completed(0, ""),
        _completed(0, ""),
        _completed(0, "ok"),
        _completed(0, ""),
        _completed(0, "bot.py\n"),
    ]

    with _patch_run(side_effects):
        state = live_tool.run_add_tool_live("rogue", fake_repo)

    assert state.aborted is not None
    assert "outside tools/" in state.aborted


def test_aborts_when_tracked_file_modified(fake_repo, clean_tools_dict):
    side_effects = [
        _completed(0, ""),
        _completed(0, ""),
        _completed(0, "ok"),
        _completed(0, "bot.py\n"),       # post tracked: claude touched a tracked file
        _completed(0, ""),
        _completed(0),                   # git checkout (revert)
    ]

    with _patch_run(side_effects):
        state = live_tool.run_add_tool_live("touch bot.py", fake_repo)

    assert state.aborted is not None
    assert "tracked files modified" in state.aborted


# --- Gate failures ---


def test_ruff_failure_blocks_approve_but_force_passes(fake_repo, clean_tools_dict):
    new_file = fake_repo / "tools" / "ping.py"

    def claude_writes(*args, **kwargs):
        new_file.write_text("import os\n")  # unused import
        return _completed(0, "ok")

    side_effects = [
        _completed(0, ""),
        _completed(0, ""),
        claude_writes,
        _completed(0, ""),
        _completed(0, "tools/ping.py\n"),
        _completed(0, "diff"),
        _completed(1, "F401 unused import"),  # ruff failure
    ]

    with _patch_run(side_effects), patch.object(
        live_tool.importlib, "import_module", side_effect=_fake_import_registers("ping", ["ping_tool"])
    ):
        state = live_tool.run_add_tool_live("add ping", fake_repo)

    assert state.gate_results["ruff"] is False
    assert state.gate_results["import"] is True

    blocked = live_tool.finalise_add_tool_live(state, "approve")
    assert not blocked.ok
    assert "validation failed" in blocked.message

    forced = live_tool.finalise_add_tool_live(state, "approve_force")
    assert forced.ok


def test_import_failure_reported_and_blocks_approve(fake_repo, clean_tools_dict):
    new_file = fake_repo / "tools" / "ping.py"

    def claude_writes(*args, **kwargs):
        new_file.write_text("# nothing\n")
        return _completed(0, "ok")

    side_effects = [
        _completed(0, ""),
        _completed(0, ""),
        claude_writes,
        _completed(0, ""),
        _completed(0, "tools/ping.py\n"),
        _completed(0, "diff"),
        _completed(0, ""),
    ]

    def fake_no_register(modname):
        return types.SimpleNamespace(__name__=modname)

    with _patch_run(side_effects), patch.object(
        live_tool.importlib, "import_module", side_effect=fake_no_register
    ):
        state = live_tool.run_add_tool_live("add nothing", fake_repo)

    assert state.gate_results["import"] is False
    assert state.registered_names == []

    blocked = live_tool.finalise_add_tool_live(state, "approve")
    assert not blocked.ok


# --- Pure helpers ---


def test_merge_into_all_idempotent():
    src = '__all__ = [\n    "a",\n    "b",\n]\n'
    out = live_tool._merge_into_all(src, ["b", "c"])
    assert out.count('"b"') == 1
    assert '"c"' in out
    out2 = live_tool._merge_into_all(out, ["c"])
    assert out2 == out


def test_append_to_init_idempotent(fake_repo):
    (fake_repo / "tools" / "newmod.py").write_text("def f():\n    pass\n")
    live_tool._append_to_init(fake_repo, "newmod", ["f"])
    first = (fake_repo / "tools" / "__init__.py").read_text()
    assert "from .newmod import f" in first
    live_tool._append_to_init(fake_repo, "newmod", ["f"])
    second = (fake_repo / "tools" / "__init__.py").read_text()
    assert first == second


def test_self_change_module_still_intact():
    assert hasattr(self_change, "run_self_change")
