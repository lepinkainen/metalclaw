import pytest

import config
import tools  # noqa: F401  — fires @tool registration side-effects
from registry import TOOLS
from tools import manual as manual_tool


@pytest.fixture
def vault(tmp_path, monkeypatch, clear_env, write_config):
    vault_dir = tmp_path / "vault"
    cfg_path = tmp_path / "config.yaml"
    write_config(cfg_path, vault_path=str(vault_dir), memory_subdir="Mem")
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    config.reset_cache()
    (vault_dir / "Mem").mkdir(parents=True)
    yield vault_dir / "Mem"
    config.reset_cache()


def test_read_manual_registered_in_TOOLS():
    assert "read_manual" in TOOLS


def test_read_manual_missing_returns_init_hint(vault):
    res = manual_tool.read_manual()
    assert res["error"] == "manual_not_initialised"
    assert "/manual init" in res["hint"]


def test_init_manual_copies_template_then_read_succeeds(vault):
    init = manual_tool.init_manual()
    assert init["status"] == "created"
    assert (vault / "manual.md").exists()

    res = manual_tool.read_manual()
    assert "toc" in res
    assert res["available_sections"]
    assert "heartbeat" in res["available_sections"]
    assert "memory-system" in res["available_sections"]


def test_init_manual_refuses_overwrite(vault):
    manual_tool.init_manual()
    original = (vault / "manual.md").read_text(encoding="utf-8")
    (vault / "manual.md").write_text(original + "\n## Custom\nuser edit\n", encoding="utf-8")

    second = manual_tool.init_manual()
    assert second["status"] == "exists"
    assert (vault / "manual.md").read_text(encoding="utf-8").endswith("user edit\n")


def test_read_manual_known_section_returns_body(vault):
    manual_tool.init_manual()
    res = manual_tool.read_manual("heartbeat")
    assert res["section"] == "heartbeat"
    assert "HEARTBEAT_OK" in res["markdown"]


def test_read_manual_case_insensitive(vault):
    manual_tool.init_manual()
    upper = manual_tool.read_manual("HEARTBEAT")
    lower = manual_tool.read_manual("heartbeat")
    assert upper["section"] == lower["section"] == "heartbeat"


def test_read_manual_unknown_section_lists_alternatives(vault):
    manual_tool.init_manual()
    res = manual_tool.read_manual("nonexistent-section")
    assert res["error"] == "unknown_section"
    assert res["requested"] == "nonexistent-section"
    assert res["available"]
    assert "memory-system" in res["available"]


def test_read_manual_tools_reference_includes_live_tool_list(vault):
    manual_tool.init_manual()
    res = manual_tool.read_manual("tools-reference")
    assert res["section"] == "tools-reference"
    body = res["markdown"]
    for name in TOOLS:
        assert f"### {name}" in body, f"tools-reference missing entry for {name}"


def test_read_manual_commands_reference_includes_manual_command(vault):
    manual_tool.init_manual()
    res = manual_tool.read_manual("slash-command-reference")
    assert res["section"] == "slash-command-reference"
    assert "/manual" in res["markdown"]
