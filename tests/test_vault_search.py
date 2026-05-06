import shutil

import pytest
import yaml

import config
import vault_search


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """Build a throwaway vault + config, point METALCLAW_CONFIG at it."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({
            "vault_path": str(vault_dir),
            "memory_subdir": "Memory",
        })
    )
    monkeypatch.setenv("METALCLAW_CONFIG", str(cfg_path))
    monkeypatch.delenv("FASTMAIL_API_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    config.reset_cache()
    yield vault_dir, cfg_path
    config.reset_cache()


def _write(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_finds_match(vault):
    vault_dir, _ = vault
    _write(vault_dir / "notes" / "foo.md", "# Foo\n\nThe quick brown fox.\n")

    result = vault_search.search("quick brown")
    paths = [h["path"] for h in result["hits"]]
    assert "notes/foo.md" in paths
    hit = next(h for h in result["hits"] if h["path"] == "notes/foo.md")
    assert hit["line_number"] == 3
    assert "quick brown fox" in hit["line"]
    assert result["truncated"] is False


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_returns_empty_for_no_match(vault):
    vault_dir, _ = vault
    _write(vault_dir / "x.md", "nothing relevant here\n")

    result = vault_search.search("zzzzzzz-no-match-string")
    assert result["hits"] == []
    assert result["truncated"] is False


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_respects_excludes(vault, monkeypatch):
    vault_dir, cfg_path = vault
    _write(vault_dir / "keep" / "a.md", "secretphrase here\n")
    _write(vault_dir / "Excluded" / "b.md", "secretphrase here\n")

    cfg_path.write_text(
        yaml.safe_dump({
            "vault_path": str(vault_dir),
            "memory_subdir": "Memory",
            "vault_search_excludes": ["Excluded/**"],
        })
    )
    config.reset_cache()

    result = vault_search.search("secretphrase")
    paths = [h["path"] for h in result["hits"]]
    assert "keep/a.md" in paths
    assert all(not p.startswith("Excluded/") for p in paths)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_only_returns_markdown(vault):
    vault_dir, _ = vault
    _write(vault_dir / "note.md", "marker line\n")
    _write(vault_dir / "page.txt", "marker line\n")

    paths = [h["path"] for h in vault_search.search("marker line")["hits"]]
    assert "note.md" in paths
    assert "page.txt" not in paths


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep not installed")
def test_search_respects_max_results(vault):
    vault_dir, _ = vault
    for i in range(5):
        _write(vault_dir / f"n{i}.md", "needle\n")

    result = vault_search.search("needle", max_results=2)
    assert len(result["hits"]) == 2
    assert result["truncated"] is True


def test_read_note_returns_body(vault):
    vault_dir, _ = vault
    body = "# Title\n\nHello world.\n"
    _write(vault_dir / "Projects" / "X.md", body)

    result = vault_search.read("Projects/X.md")
    assert result["path"] == "Projects/X.md"
    assert result["body"] == body
    assert result["truncated"] is False


def test_read_note_rejects_path_traversal(vault):
    with pytest.raises(ValueError, match="outside the vault"):
        vault_search.read("../etc/passwd")


def test_read_note_rejects_non_markdown(vault):
    vault_dir, _ = vault
    _write(vault_dir / "img.png", "binary placeholder")
    with pytest.raises(ValueError, match="markdown"):
        vault_search.read("img.png")


def test_read_note_missing_file(vault):
    with pytest.raises(FileNotFoundError):
        vault_search.read("does-not-exist.md")


def test_read_note_truncates_oversized_body(vault, monkeypatch):
    vault_dir, _ = vault
    monkeypatch.setattr(vault_search, "_BODY_CHAR_LIMIT", 16)
    _write(vault_dir / "big.md", "x" * 100)

    result = vault_search.read("big.md")
    assert result["truncated"] is True
    assert len(result["body"]) == 16
