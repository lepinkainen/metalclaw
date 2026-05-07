import pytest

import config
import memory


@pytest.fixture
def vault(tmp_path, monkeypatch, clear_env, write_config):
    """Build a throwaway config + vault, point METALCLAW_CONFIG at it, clear caches."""
    vault_dir = tmp_path / "vault"
    cfg_path = tmp_path / "config.yaml"
    write_config(
        cfg_path,
        vault_path=str(vault_dir),
        memory_subdir="Memory",
        fastmail_api_token="test-token",
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    config.reset_cache()
    yield vault_dir / "Memory"
    config.reset_cache()


def test_load_creates_empty_when_missing(vault):
    mem = memory.load()
    assert mem.preferences == {}
    assert mem.facts == []


def test_set_preference_persists_and_round_trips(vault):
    memory.set_preference("role", "engineer")
    memory.set_preference("tone", "terse")
    mem = memory.load()
    assert mem.preferences == {"role": "engineer", "tone": "terse"}
    file = vault / "memory.md"
    assert file.exists()
    raw = file.read_text()
    assert "## Preferences" in raw
    assert "**role**: engineer" in raw


def test_set_preference_upserts(vault):
    memory.set_preference("tone", "terse")
    memory.set_preference("tone", "ultra-terse")
    assert memory.load().preferences == {"tone": "ultra-terse"}


def test_add_fact_appends_and_dedupes(vault):
    memory.add_fact("loves [[trains]]")
    memory.add_fact("loves [[trains]]")
    memory.add_fact("works at [[Metacore]]")
    assert memory.load().facts == ["loves [[trains]]", "works at [[Metacore]]"]


def test_add_instruction_appends_and_dedupes(vault):
    memory.add_instruction("Reply in Finnish unless asked otherwise.")
    memory.add_instruction("Reply in Finnish unless asked otherwise.")
    memory.add_instruction("Use metric units.")
    assert memory.load().instructions == [
        "Reply in Finnish unless asked otherwise.",
        "Use metric units.",
    ]


def test_forget_unique_instruction(vault):
    memory.add_instruction("Reply in Finnish unless asked otherwise.")
    memory.add_instruction("Use metric units.")
    res = memory.forget("metric")
    assert res.status == "removed"
    assert res.entry == "[instruction] Use metric units."
    assert memory.load().instructions == ["Reply in Finnish unless asked otherwise."]


def test_summary_includes_instructions(vault):
    memory.add_instruction("Reply in Finnish.")
    s = memory.summary()
    assert "instructions:" in s
    assert "Reply in Finnish." in s


def test_forget_unique_preference(vault):
    memory.set_preference("role", "engineer")
    memory.set_preference("tone", "terse")
    res = memory.forget("role")
    assert res.status == "removed"
    assert res.entry == "[pref] **role**: engineer"
    assert memory.load().preferences == {"tone": "terse"}


def test_forget_fact_unique_substring_match(vault):
    memory.add_fact("commutes via [[R-train]]")
    memory.add_fact("drinks coffee")
    res = memory.forget("R-TRAIN")
    assert res.status == "removed"
    assert res.entry == "[fact] commutes via [[R-train]]"
    assert memory.load().facts == ["drinks coffee"]


def test_forget_not_found(vault):
    memory.set_preference("role", "engineer")
    res = memory.forget("zzz")
    assert res.status == "not_found"
    assert res.entry is None
    assert res.matches == []


def test_forget_ambiguous_returns_candidates_and_deletes_nothing(vault):
    memory.set_preference("role", "engineer")
    memory.set_preference("tone", "terse")
    memory.add_fact("drinks coffee")
    res = memory.forget("e")
    assert res.status == "ambiguous"
    assert len(res.matches) >= 2
    assert all(m.startswith(("[pref] ", "[fact] ", "[instruction] ")) for m in res.matches)
    mem = memory.load()
    assert mem.preferences == {"role": "engineer", "tone": "terse"}
    assert mem.facts == ["drinks coffee"]


def test_forget_does_not_cross_buckets_silently(vault):
    """Same text in pref-value and fact must both be reported as ambiguous, never auto-pick."""
    memory.set_preference("role", "engineer")
    memory.add_fact("engineer at [[Metacore]]")
    res = memory.forget("engineer")
    assert res.status == "ambiguous"
    assert len(res.matches) == 2


def test_summary_empty_when_nothing_stored(vault):
    assert memory.summary() == ""


def test_summary_includes_preferences_and_facts(vault):
    memory.set_preference("role", "engineer")
    memory.add_fact("uses Ollama")
    s = memory.summary()
    assert "role=engineer" in s
    assert "uses Ollama" in s


def test_summary_truncates_with_hint(vault):
    long_value = "x" * 1000
    memory.set_preference("role", long_value)
    s = memory.summary(max_chars=200)
    assert len(s) <= 200
    assert "get_user_memory" in s


def test_render_full_yaml_frontmatter_round_trips(vault):
    memory.set_preference("role", "[[engineer]]")
    memory.add_fact("primary mailbox is [[Inbox]]")
    raw = memory.render_full()
    mem = memory.load()
    assert mem.preferences["role"] == "[[engineer]]"
    assert "primary mailbox is [[Inbox]]" in mem.facts
    assert raw.startswith("---\nupdated:")


def test_atomic_write_preserves_original_on_replace_failure(vault, monkeypatch):
    memory.set_preference("role", "engineer")
    original = (vault / "memory.md").read_text()

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("memory.os.replace", boom)
    with pytest.raises(OSError):
        memory.set_preference("tone", "terse")

    assert (vault / "memory.md").read_text() == original
    assert list(vault.glob(".memory-*")) == []


def test_single_file_no_per_scope_split(vault):
    memory.set_preference("role", "engineer")
    memory.add_fact("uses [[Ollama]]")
    files = sorted(p.name for p in vault.iterdir() if p.is_file())
    md_files = [f for f in files if f.endswith(".md")]
    assert md_files == ["memory.md"]


def test_migrate_legacy_scopes_merges_and_renames(vault):
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "cli.md").write_text(
        "---\nscope: cli\nupdated: 2026-05-06T00:00:00+00:00\n---\n\n"
        "## Preferences\n- **role**: Software Developer\n- **tone**: terse\n\n"
        "## Facts\n- uses [[Ollama]]\n\n## Instructions\n",
        encoding="utf-8",
    )
    (vault / "telegram-12345.md").write_text(
        "---\nscope: telegram-12345\nupdated: 2026-05-06T00:00:00+00:00\n---\n\n"
        "## Preferences\n- **tone**: ultra-terse\n\n"
        "## Facts\n- uses [[Ollama]]\n- commutes via [[R-train]]\n\n## Instructions\n",
        encoding="utf-8",
    )

    migrated = memory.migrate_legacy_scopes()

    assert sorted(migrated) == ["cli.md", "telegram-12345.md"]
    mem = memory.load()
    assert mem.preferences["role"] == "Software Developer"
    assert mem.preferences["tone"] in ("terse", "ultra-terse")
    assert mem.facts == ["uses [[Ollama]]", "commutes via [[R-train]]"]

    assert (vault / "cli.md.bak").exists()
    assert (vault / "telegram-12345.md.bak").exists()
    assert not (vault / "cli.md").exists()
    assert not (vault / "telegram-12345.md").exists()


def test_migrate_legacy_scopes_idempotent_when_nothing_to_migrate(vault):
    vault.mkdir(parents=True, exist_ok=True)
    memory.set_preference("role", "engineer")
    assert memory.migrate_legacy_scopes() == []


def test_load_cache_avoids_reparse_until_mtime_changes(vault, monkeypatch):
    memory.set_preference("role", "engineer")
    memory.load()  # warm cache

    parse_calls = {"n": 0}
    real_parse = memory._parse

    def counting_parse(text):
        parse_calls["n"] += 1
        return real_parse(text)

    monkeypatch.setattr("memory._parse", counting_parse)

    for _ in range(5):
        memory.load()
    assert parse_calls["n"] == 0, "expected mtime cache hit, got re-parse"

    memory.set_preference("tone", "terse")  # triggers _write_locked → cache invalidate
    memory.load()
    assert parse_calls["n"] >= 1, "cache should have been invalidated by write"


def test_load_returns_independent_copies_so_callers_cannot_pollute_cache(vault):
    memory.set_preference("role", "engineer")
    a = memory.load()
    a.preferences["role"] = "MUTATED"
    a.facts.append("injected")
    b = memory.load()
    assert b.preferences == {"role": "engineer"}
    assert b.facts == []
