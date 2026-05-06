import yaml
import pytest

import config
import memory


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Build a throwaway config + vault, point METALCLAW_CONFIG at it, clear caches."""
    vault_dir = tmp_path / "vault"
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "vault_path": str(vault_dir),
            "memory_subdir": "Memory",
            "fastmail_api_token": "test-token",
        })
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    monkeypatch.delenv("FASTMAIL_API_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    config.reset_cache()
    memory.current_scope.set("test")
    yield vault_dir / "Memory"
    config.reset_cache()


def test_load_creates_empty_when_missing(vault):
    mem = memory.load("test")
    assert mem.scope == "test"
    assert mem.preferences == {}
    assert mem.facts == []


def test_set_preference_persists_and_round_trips(vault):
    memory.set_preference("role", "engineer", scope="test")
    memory.set_preference("tone", "terse", scope="test")
    mem = memory.load("test")
    assert mem.preferences == {"role": "engineer", "tone": "terse"}
    file = vault / "test.md"
    assert file.exists()
    raw = file.read_text()
    assert "## Preferences" in raw
    assert "**role**: engineer" in raw


def test_set_preference_upserts(vault):
    memory.set_preference("tone", "terse", scope="test")
    memory.set_preference("tone", "ultra-terse", scope="test")
    assert memory.load("test").preferences == {"tone": "ultra-terse"}


def test_add_fact_appends_and_dedupes(vault):
    memory.add_fact("loves [[trains]]", scope="test")
    memory.add_fact("loves [[trains]]", scope="test")
    memory.add_fact("works at [[Metacore]]", scope="test")
    assert memory.load("test").facts == ["loves [[trains]]", "works at [[Metacore]]"]


def test_forget_preference(vault):
    memory.set_preference("role", "engineer", scope="test")
    memory.set_preference("tone", "terse", scope="test")
    assert memory.forget("role", scope="test") is True
    assert memory.load("test").preferences == {"tone": "terse"}


def test_forget_fact_substring(vault):
    memory.add_fact("commutes via [[R-train]]", scope="test")
    memory.add_fact("drinks coffee", scope="test")
    assert memory.forget("R-TRAIN", scope="test") is True
    assert memory.load("test").facts == ["drinks coffee"]


def test_forget_returns_false_when_no_match(vault):
    memory.set_preference("role", "engineer", scope="test")
    assert memory.forget("zzz", scope="test") is False


def test_summary_empty_when_nothing_stored(vault):
    assert memory.summary("test") == ""


def test_summary_includes_preferences_and_facts(vault):
    memory.set_preference("role", "engineer", scope="test")
    memory.add_fact("uses Ollama", scope="test")
    s = memory.summary("test")
    assert "role=engineer" in s
    assert "uses Ollama" in s


def test_summary_truncates_at_max_chars(vault):
    long_value = "x" * 1000
    memory.set_preference("role", long_value, scope="test")
    s = memory.summary("test", max_chars=50)
    assert len(s) <= 50
    assert s.endswith("…")


def test_render_full_yaml_frontmatter_round_trips(vault):
    memory.set_preference("role", "[[engineer]]", scope="test")
    memory.add_fact("primary mailbox is [[Inbox]]", scope="test")
    raw = memory.render_full("test")
    # Re-parse should preserve entries
    mem = memory.load("test")
    assert mem.preferences["role"] == "[[engineer]]"
    assert "primary mailbox is [[Inbox]]" in mem.facts
    assert raw.startswith("---\nscope: test")
